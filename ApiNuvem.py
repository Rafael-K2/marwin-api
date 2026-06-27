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
_ws_clientes_refeitorio = set()   # clientes WebSocket conectados ao painel TV

ADMIN_PASSWORD = os.getenv("MARWIN_ADMIN_PASS", "Marwin2026")

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


def checar_senha(req):
    hdr = req.headers.get("X-Senha", "")
    if not hdr:
        return False
    if BCRYPT_AVAILABLE and isinstance(ADMIN_PASSWORD, str) and ADMIN_PASSWORD.startswith("$2"):
        try:
            return bcrypt.checkpw(hdr.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
        except Exception:
            return False
    return secrets.compare_digest(hdr, ADMIN_PASSWORD)


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
    except Exception:
        pass
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