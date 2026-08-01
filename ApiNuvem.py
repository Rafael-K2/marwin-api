"""
API MARWIN — Nuvem (Opção B)
============================
API mínima para o index.html gravar no Neon sem o PC da escola ligado.

Deploy (ex.: Render, Railway):
  pip install flask flask-cors psycopg2-binary gunicorn
  gunicorn ApiNuvem:app --bind 0.0.0.0:$PORT

Variáveis de ambiente:
  MARWIN_DATABASE_URL  — connection string do Neon
  MARWIN_ADMIN_PASS      — senha para rotas /admin/* (sync do painel local)
  PORT                   — porta (padrão 8080)
"""

import os
import sys
import subprocess
import logging
import datetime
import time
from pathlib import Path
from collections import defaultdict

for pkg in ["flask", "flask-cors", "psycopg2-binary", "flask-sock"]:
    try:
        __import__(pkg.replace("-", "_"))
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet"])

from flask import Flask, request, jsonify
from flask_cors import CORS
import secrets

import marwin_db as db
from flask_sock import Sock

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("marwin.api")

app = Flask(__name__)
CORS(app)
sock = Sock(app)
# ⚠️ Este conjunto fica em memória e só funciona corretamente com um único worker.
# Se a carga crescer, migre para Redis pub/sub ou outro broker para fan-out.
_ws_clientes_refeitorio = set()   # clientes WebSocket conectados ao painel TV

def _senha_admin_fallback():
    base_dir = Path(__file__).resolve().parents[1]
    senha_path = base_dir / "Servidor" / "dados" / "senha_admin.txt"
    senha_path.parent.mkdir(parents=True, exist_ok=True)
    if senha_path.exists():
        return senha_path.read_text(encoding="utf-8").strip()
    senha = secrets.token_urlsafe(18)
    senha_path.write_text(senha, encoding="utf-8")
    return senha

ADMIN_PASSWORD = os.getenv("MARWIN_ADMIN_PASS")
if not ADMIN_PASSWORD:
    if os.getenv("MARWIN_ALLOW_DEFAULT_ADMIN") == "1":
        ADMIN_PASSWORD = _senha_admin_fallback()
        logger.warning("MARWIN_ADMIN_PASS não definido; usando senha local gerada em Servidor/dados/senha_admin.txt")
    else:
        raise RuntimeError("MARWIN_ADMIN_PASS deve ser definida para iniciar a API")

try:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    if os.getenv("SENTRY_DSN"):
        sentry_sdk.init(
            dsn=os.getenv("SENTRY_DSN"),
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        )
except Exception:
    sentry_sdk = None

RATE_LIMIT_BUCKETS = defaultdict(list)
PROFILE_TOKENS = {
    "ADM": os.getenv("MARWIN_ADM_TOKEN") or os.getenv("MARWIN_ADMIN_TOKEN"),
    "COOR": os.getenv("MARWIN_COOR_TOKEN"),
    "SERC": os.getenv("MARWIN_SERC_TOKEN"),
    "REFEITORIO": os.getenv("MARWIN_REFEITORIO_TOKEN"),
}

# Timestamp do último dado inserido — usado pelo polling do index.html
_ultimo_update_ts = None

@app.route("/ultimo-update", methods=["GET"])
def ultimo_update():
    return jsonify({"ts": _ultimo_update_ts})

try:
    import bcrypt
    BCRYPT_AVAILABLE = True
except Exception:
    BCRYPT_AVAILABLE = False


def _excedeu_rate_limit(req):
    if not request.path.startswith("/admin/"):
        return False
    now = time.monotonic()
    bucket = f"{req.remote_addr or 'unknown'}:{req.headers.get('X-Senha', '')[:8]}"
    timestamps = RATE_LIMIT_BUCKETS[bucket]
    timestamps[:] = [t for t in timestamps if now - t < 60]
    if len(timestamps) >= 30:
        return True
    timestamps.append(now)
    return False


@app.before_request
def _aplicar_rate_limit_admin():
    if _excedeu_rate_limit(request):
        return jsonify({"erro": "Muitas tentativas de acesso. Tente novamente mais tarde."}), 429


def checar_senha(req):
    hdr = req.headers.get("X-Senha", "")
    if hdr:
        if BCRYPT_AVAILABLE and isinstance(ADMIN_PASSWORD, str) and ADMIN_PASSWORD.startswith("$2"):
            try:
                return bcrypt.checkpw(hdr.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
            except Exception:
                return False
        if secrets.compare_digest(hdr, ADMIN_PASSWORD):
            return True

    perfil = (req.headers.get("X-Perfil", "") or "").strip().upper()
    token = (req.headers.get("X-Token", "") or "").strip()
    if perfil and token:
        expected = PROFILE_TOKENS.get(perfil)
        if expected and secrets.compare_digest(token, expected):
            return True
    return False


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "servico": "marwin-api"})


@app.route("/cardapio", methods=["GET"])
def get_cardapio():
    return jsonify(db.ler_config_kv("cardapio", db.CARDAPIO_PADRAO))


@app.route("/eventos", methods=["GET"])
def get_eventos():
    return jsonify(db.ler_config_kv("eventos", db.EVENTOS_PADRAO))


@app.route("/config", methods=["GET"])
def get_config():
    return jsonify(db.ler_config_kv("config", db.CONFIG_PADRAO))


@app.route("/admin/cardapio", methods=["PUT"])
def put_cardapio():
    if not checar_senha(request):
        return jsonify({"erro": "Acesso negado"}), 403
    dados = request.get_json()
    if dados is None:
        return jsonify({"erro": "JSON invalido"}), 400
    try:
        db.salvar_config_kv("cardapio", dados)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Erro ao salvar cardapio: {e}")
        return jsonify({"erro": str(e)}), 500


@app.route("/admin/eventos", methods=["PUT"])
def put_eventos():
    if not checar_senha(request):
        return jsonify({"erro": "Acesso negado"}), 403
    dados = request.get_json()
    if not isinstance(dados, list):
        return jsonify({"erro": "Lista de eventos esperada"}), 400
    try:
        db.salvar_config_kv("eventos", dados)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Erro ao salvar eventos: {e}")
        return jsonify({"erro": str(e)}), 500


@app.route("/admin/config", methods=["PUT"])
def put_config():
    if not checar_senha(request):
        return jsonify({"erro": "Acesso negado"}), 403
    dados = request.get_json()
    if dados is None:
        return jsonify({"erro": "JSON invalido"}), 400
    try:
        db.salvar_config_kv("config", dados)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Erro ao salvar config: {e}")
        return jsonify({"erro": str(e)}), 500


FUSO_BRASIL = datetime.timezone(datetime.timedelta(hours=-3))

def _hoje_br():
    """Retorna a data atual no fuso horário do Brasil (UTC-3)."""
    return datetime.datetime.now(FUSO_BRASIL).date()


@app.route("/avaliacao/verificar", methods=["GET"])
def verificar_avaliacao():
    nome = request.args.get("nome", "").strip()
    if not nome or nome.lower() in {"anonimo", "anônimo"}:
        return jsonify({"ja_avaliou": False}), 200
    hoje = _hoje_br()
    try:
        ja = db.avaliacao_ja_existe_db(nome, hoje.isocalendar()[1], hoje.isocalendar()[0])
        return jsonify({"ja_avaliou": ja}), 200
    except RuntimeError:
        return jsonify({"erro": "Banco de dados indisponível"}), 503


@app.route("/avaliacao", methods=["POST"])
def post_avaliacao():
    dados = request.get_json()
    if not dados:
        return jsonify({"erro": "JSON invalido"}), 400
    nome = dados.get("nome", "Anonimo")
    serie = dados.get("serie", "N/A")
    curso = dados.get("curso", "N/A")
    respostas = dados.get("respostas", {})

    if nome and nome.strip().lower() not in {"anonimo", "anônimo"}:
        hoje = _hoje_br()
        try:
            if db.avaliacao_ja_existe_db(nome, hoje.isocalendar()[1], hoje.isocalendar()[0]):
                return jsonify({"status": "ja_avaliou", "mensagem": "Você já avaliou esta semana"}), 200
        except RuntimeError:
            return jsonify({"erro": "Banco de dados indisponível"}), 503

    data_hora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    try:
        for chave, nota in respostas.items():
            estagio, item = chave.split("|", 1)
            db.inserir_avaliacao_db([data_hora, nome, serie, curso, estagio, item, nota])
    except RuntimeError:
        return jsonify({"erro": "Banco de dados indisponível"}), 503
    except Exception as e:
        logger.error(f"Erro ao salvar avaliação: {e}")
        return jsonify({"erro": "Erro ao salvar avaliação"}), 500
    global _ultimo_update_ts
    _ultimo_update_ts = datetime.datetime.now().isoformat()
    logger.info(f"Avaliação: {nome} ({len(respostas)} itens)")
    return jsonify({"status": "ok"})


@app.route("/refeitorio/registrar", methods=["POST"])
def registrar_refeicao():
    dados = request.get_json()
    if not dados:
        return jsonify({"erro": "JSON invalido"}), 400

    matricula = db.limpar_campo_usb(dados.get("matricula", "").strip())
    refeicao = dados.get("refeicao", "almoco").strip().lower()
    nome = db.limpar_campo_usb(dados.get("nome", "Desconhecido").strip()) or "Desconhecido"
    serie = db.limpar_campo_usb(dados.get("serie", "N/A").strip()) or "N/A"
    curso = db.limpar_campo_usb(dados.get("curso", "N/A").strip()) or "N/A"

    if not matricula:
        return jsonify({"erro": "Matricula nao informada"}), 400

    dup = db.refeitorio_duplicado_db(matricula, refeicao)
    if dup:
        return jsonify({
            "status": "ja_registrado",
            "nome": dup["nome"],
            "hora": dup["hora"],
            "total_hoje": dup["total_hoje"],
            "total_refeicao": dup["total_refeicao"],
        }), 200

    hora = datetime.datetime.now().strftime("%H:%M:%S")
    registro = [db.hoje(), hora, matricula, nome, serie, curso, refeicao]
    try:
        db.inserir_refeitorio_db(registro)
    except RuntimeError:
        return jsonify({"erro": "Banco de dados indisponível"}), 503

    global _ultimo_update_ts
    _ultimo_update_ts = datetime.datetime.now().isoformat()
    _broadcast_refeitorio()
    registros = db.ler_refeitorio_hoje_db()
    total_refeicao = sum(1 for r in registros if r[6] == refeicao)
    return jsonify({
        "status": "ok",
        "nome": nome,
        "hora": hora,
        "aula": db.aula_por_hora(hora),
        "total_hoje": len(registros),
        "total_refeicao": total_refeicao,
    }), 200


@app.route("/frequencia/registrar", methods=["POST"])
def registrar_frequencia():
    dados = request.get_json()
    if not dados:
        return jsonify({"erro": "JSON invalido"}), 400

    matricula = db.limpar_campo_usb(dados.get("matricula", "").strip())
    nome = db.limpar_campo_usb(dados.get("nome", "Desconhecido").strip()) or "Desconhecido"
    serie = db.limpar_campo_usb(dados.get("serie", "N/A").strip()) or "N/A"
    curso = db.limpar_campo_usb(dados.get("curso", "N/A").strip()) or "N/A"

    if not matricula:
        return jsonify({"erro": "Matricula nao informada"}), 400

    dup = db.frequencia_duplicado_db(matricula)
    if dup:
        return jsonify({
            "status": "ja_registrado",
            "nome": dup["nome"],
            "hora": dup["hora"],
            "total_hoje": dup["total_hoje"],
        }), 200

    hora = datetime.datetime.now().strftime("%H:%M:%S")
    registro = [db.hoje(), hora, matricula, nome, serie, curso, db.aula_por_hora(hora)]
    try:
        db.inserir_frequencia_db(registro)
    except RuntimeError:
        return jsonify({"erro": "Banco de dados indisponível"}), 503

    global _ultimo_update_ts
    _ultimo_update_ts = datetime.datetime.now().isoformat()
    registros = db.ler_frequencia_hoje_db()
    return jsonify({
        "status": "ok",
        "nome": nome,
        "hora": hora,
        "aula": registro[6],
        "total_hoje": len(registros),
    }), 200


def _total_alunos():
    """Retorna o total de alunos cadastrados para calcular ausências no TV.

    Ordem de prioridade:
    1. lista_alunos sincronizada via /admin/lista-alunos (mais precisa)
    2. Matrículas únicas históricas da tabela frequencia (boa aproximação)
    3. None — o chamador usa entraram como total (sem mostrar ausências)
    """
    try:
        lista = db.ler_config_kv("lista_alunos", [])
        if isinstance(lista, list) and len(lista) > 0:
            return len(lista)
    except Exception:
        pass
    try:
        rows = db.executar_pg(
            "SELECT COUNT(DISTINCT matricula) AS total FROM frequencia",
            (), fetch=True
        )
        if rows and rows[0].get("total", 0) > 0:
            return int(rows[0]["total"])
    except Exception:
        pass
    return None


def _payload_refeitorio():
    """Monta o JSON que o tv.html espera."""
    registros = db.ler_refeitorio_hoje_db()
    matriculas_unicas = {r[2] for r in registros if r[2]}
    entraram = len(matriculas_unicas)
    total = _total_alunos() or entraram
    nao_entraram = max(total - entraram, 0)
    return {"entraram": entraram, "naoEntraram": nao_entraram, "total": total}


def _broadcast_refeitorio():
    """Envia dados atualizados para todos os clientes WS conectados."""
    import json
    mortos = set()
    dados = json.dumps(_payload_refeitorio())
    for ws in _ws_clientes_refeitorio:
        try:
            ws.send(dados)
        except Exception:
            mortos.add(ws)
    _ws_clientes_refeitorio.difference_update(mortos)


@app.route("/refeitorio/hoje", methods=["GET"])
def refeitorio_hoje():
    """Rota REST para o tv.html (modo polling).

    Retorna: { "entraram": N, "naoEntraram": N, "total": N }
    """
    try:
        return jsonify(_payload_refeitorio())
    except RuntimeError:
        return jsonify({"erro": "Banco de dados indisponivel"}), 503


@sock.route("/ws/refeitorio")
def ws_refeitorio(ws):
    """WebSocket para o tv.html (modo tempo real).

    O cliente se conecta e recebe dados sempre que houver novo registro.
    Também recebe um push imediato ao conectar.
    """
    import json
    _ws_clientes_refeitorio.add(ws)
    try:
        # Envia estado atual imediatamente ao conectar
        ws.send(json.dumps(_payload_refeitorio()))
        # Mantém conexão viva aguardando mensagens (ping/keep-alive do cliente)
        while True:
            msg = ws.receive(timeout=30)
            if msg is None:
                break  # cliente desconectou
    except Exception as exc:
        logger.warning(f"WebSocket do refeitório encerrado: {exc}")
    finally:
        _ws_clientes_refeitorio.discard(ws)


@app.route("/admin/lista-alunos", methods=["PUT"])
def put_lista_alunos():
    """Sincroniza a lista de alunos do painel desktop para o Neon."""
    if not checar_senha(request):
        return jsonify({"erro": "Acesso negado"}), 403
    dados = request.get_json()
    if not isinstance(dados, list):
        return jsonify({"erro": "Lista esperada"}), 400
    try:
        db.salvar_config_kv("lista_alunos", dados)
        logger.info(f"Lista de alunos sincronizada: {len(dados)} alunos")
        return jsonify({"status": "ok", "total": len(dados)})
    except Exception as e:
        logger.error(f"Erro ao salvar lista_alunos: {e}")
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    db.iniciar_pool_pg()
    try:
        db.criar_tabelas()
    except Exception as e:
        logger.warning(f"Tabelas: {e}")
    port = int(os.getenv("PORT", "8080"))
    logger.info(f"API MARWIN na porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
    
@app.route("/rotas", methods=["GET"])
def listar_rotas():
    return jsonify(
        sorted([str(r) for r in app.url_map.iter_rules()])
    )