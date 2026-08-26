"""
Camada compartilhada de banco e regras de negócio — MARWIN.
Usada por ApiNuvem.py (nuvem) e ServidorV15.py (admin local).
"""

import os
import json
import re
import logging
import datetime

logger = logging.getLogger("marwin")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DADOS_DIR = os.path.join(_BASE_DIR, "dados")
DB_CONFIG_FILE = os.path.join(DADOS_DIR, "db_config.json")

# ⚠️  NUNCA coloque a connection string diretamente aqui.
# Configure a variável de ambiente MARWIN_DATABASE_URL (ou DATABASE_URL)
# no sistema operacional, no Railway, ou no Render antes de rodar.
DB_DEFAULT_CONNECTION = os.getenv(
    "MARWIN_DATABASE_URL",
    os.getenv("DATABASE_URL", ""),   # vazio = falha clara em vez de conectar com credenciais antigas
)

PG_POOL = None

CARDAPIO_PADRAO = {
    "SEGUNDA": ["Cuzcuz com ovo e cafe fresco", "Arroz, feijao, frango cozido e batata doce", "Pao com manteiga e vitamina de goiaba"],
    "TERCA": ["Cuzcuz com ovo e cafe fresco", "Arroz, feijao, frango cozido e batata doce", "Pao com manteiga e vitamina de goiaba"],
    "QUARTA": ["Cuzcuz com ovo e cafe fresco", "Arroz, feijao, frango cozido e batata doce", "Pao com manteiga e vitamina de goiaba"],
    "QUINTA": ["Cuzcuz com ovo e cafe fresco", "Arroz, feijao, frango cozido e batata doce", "Pao com manteiga e vitamina de goiaba"],
    "SEXTA": ["Cuzcuz com ovo e cafe fresco", "Arroz, feijao, frango cozido e batata doce", "Pao com manteiga e vitamina de goiaba"],
}
EVENTOS_PADRAO = [
    {"data": "03/02", "evento": "Inicio das Aulas"},
    {"data": "01/05", "evento": "Feriado: Dia do Trabalhador"},
    {"data": "24/06", "evento": "Arraia do Marwin"},
    {"data": "07/09", "evento": "Feriado: Independencia do Brasil"},
    {"data": "15/10", "evento": "Dia do Professor"},
    {"data": "25/12", "evento": "Natal"},
]
CONFIG_PADRAO = {"avaliacoes_ativas": True, "modo_leitura": "camera", "painel_avisos": []}


def ler_json(path, padrao):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return padrao


def _carregar_db_config():
    os.makedirs(DADOS_DIR, exist_ok=True)
    cfg = ler_json(DB_CONFIG_FILE, {})
    if not cfg.get("connection_string"):
        cfg["connection_string"] = DB_DEFAULT_CONNECTION
        with open(DB_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def iniciar_pool_pg():
    global PG_POOL
    if PG_POOL is not None:
        return
    cfg = _carregar_db_config()
    try:
        from psycopg2 import pool
        PG_POOL = pool.ThreadedConnectionPool(1, 10, cfg["connection_string"], connect_timeout=10)
        logger.info("Pool PostgreSQL inicializado")
    except Exception as e:
        PG_POOL = None
        logger.warning(f"PostgreSQL indisponível: {e}")


def get_pg_conn():
    global PG_POOL
    if PG_POOL is None:
        iniciar_pool_pg()
    if PG_POOL is None:
        return None
    try:
        return PG_POOL.getconn()
    except Exception:
        return None


def _release_pg_conn(conn):
    global PG_POOL
    if not conn:
        return
    try:
        if PG_POOL:
            PG_POOL.putconn(conn)
        else:
            conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def executar_pg(sql, params=None, fetch=False):
    conn = get_pg_conn()
    if conn is None:
        raise RuntimeError("PostgreSQL indisponível")
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if fetch:
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            return [dict(zip(cols, row)) for row in rows]
        conn.commit()
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        _release_pg_conn(conn)


def criar_tabelas():
    executar_pg(
        """
        CREATE TABLE IF NOT EXISTS refeitorio (
            data TEXT NOT NULL,
            horaentrada TEXT NOT NULL,
            matricula TEXT NOT NULL,
            nome TEXT NOT NULL,
            serie TEXT,
            curso TEXT,
            refeicao TEXT
        );
        """
    )
    executar_pg("CREATE INDEX IF NOT EXISTS idx_refeitorio_data ON refeitorio (data)")
    executar_pg("CREATE INDEX IF NOT EXISTS idx_refeitorio_data_matricula ON refeitorio (data, matricula)")
    executar_pg(
        """
        CREATE TABLE IF NOT EXISTS frequencia (
            data TEXT NOT NULL,
            horaentrada TEXT NOT NULL,
            matricula TEXT NOT NULL,
            nome TEXT NOT NULL,
            serie TEXT,
            curso TEXT,
            aula TEXT
        );
        """
    )
    executar_pg("CREATE INDEX IF NOT EXISTS idx_frequencia_data ON frequencia (data)")
    executar_pg("CREATE INDEX IF NOT EXISTS idx_frequencia_data_matricula ON frequencia (data, matricula)")
    executar_pg(
        """
        CREATE TABLE IF NOT EXISTS avaliacoes (
            data TEXT NOT NULL,
            aluno TEXT NOT NULL,
            serie TEXT,
            curso TEXT,
            estagio TEXT,
            item TEXT,
            nota TEXT
        );
        """
    )
    executar_pg("CREATE INDEX IF NOT EXISTS idx_avaliacoes_data ON avaliacoes (data)")
    executar_pg("CREATE INDEX IF NOT EXISTS idx_avaliacoes_data_aluno ON avaliacoes (data, aluno)")
    executar_pg(
        """
        CREATE TABLE IF NOT EXISTS sistema_config (
            chave TEXT PRIMARY KEY,
            valor JSONB NOT NULL
        );
        """
    )
    # Respostas da gestão às avaliações semanais dos alunos. Antes a
    # comunicação era só de mão única (aluno avalia, ninguém responde).
    # `setor` é um dos 4 setores avaliados (Comida/Limpeza/Ensino/
    # Acolhimento) ou "Geral" para um recado que não é sobre um setor
    # específico. Visível a todos os alunos no app (não é uma resposta
    # individual — a maioria das avaliações é anônima ou agregada).
    executar_pg(
        """
        CREATE TABLE IF NOT EXISTS respostas_gestao (
            id SERIAL PRIMARY KEY,
            criado_em TEXT NOT NULL,
            setor TEXT NOT NULL DEFAULT 'Geral',
            titulo TEXT NOT NULL,
            mensagem TEXT NOT NULL,
            autor TEXT
        );
        """
    )
    executar_pg("CREATE INDEX IF NOT EXISTS idx_respostas_gestao_criado_em ON respostas_gestao (criado_em)")


def hoje():
    return datetime.date.today().strftime("%d/%m/%Y")


def aula_por_hora(hora):
    try:
        hora_limpa = hora.strip().split(" ")[0]
        if len(hora_limpa) == 5:
            hora_limpa += ":00"
        t = datetime.datetime.strptime(hora_limpa, "%H:%M:%S").time()
    except Exception:
        return "Fora do horário"

    periodos = [
        ("07:10:00", "08:00:00", "Aula 1"),
        ("08:00:00", "08:50:00", "Aula 2"),
        ("08:50:00", "09:10:00", "Intervalo"),
        ("09:10:00", "10:00:00", "Aula 3"),
        ("10:00:00", "10:50:00", "Aula 4"),
        ("10:50:00", "11:40:00", "Aula 5"),
        ("11:40:00", "13:00:00", "Intervalo"),
        ("13:00:00", "13:50:00", "Aula 6"),
        ("13:50:00", "14:40:00", "Aula 7"),
        ("14:40:00", "15:00:00", "Intervalo"),
        ("15:00:00", "15:50:00", "Aula 8"),
        ("15:50:00", "16:40:00", "Aula 9"),
    ]
    for inicio, fim, nome_aula in periodos:
        if datetime.time.fromisoformat(inicio) <= t < datetime.time.fromisoformat(fim):
            return nome_aula
    return "Fora do horário"


def limpar_campo_usb(valor: str) -> str:
    if not valor:
        return valor
    v = valor
    if "^" in v or "Ç" in v:
        v = v.replace("^Ç", ":").replace("^", "").replace("Ç", "")
    return re.sub(r"[^a-zA-Z0-9À-ÿ\s.'-]", "", v).strip()


def ler_config_kv(chave, padrao):
    try:
        rows = executar_pg(
            "SELECT valor FROM sistema_config WHERE chave = %s",
            (chave,),
            fetch=True,
        )
        if rows:
            return rows[0]["valor"]
    except Exception as e:
        logger.warning(f"Falha ao ler config {chave}: {e}")
    return padrao


def salvar_config_kv(chave, valor):
    from psycopg2.extras import Json
    executar_pg(
        """
        INSERT INTO sistema_config (chave, valor) VALUES (%s, %s)
        ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
        """,
        (chave, Json(valor)),
    )


def ler_refeitorio_hoje_db():
    rows = executar_pg(
        "SELECT data, horaentrada, matricula, nome, serie, curso, refeicao FROM refeitorio WHERE data = %s",
        (hoje(),),
        fetch=True,
    )
    if not rows:
        return []
    return [
        [row["data"], row["horaentrada"], row["matricula"], row["nome"], row["serie"], row["curso"], row["refeicao"]]
        for row in rows
    ]


def inserir_refeitorio_db(registro):
    executar_pg(
        "INSERT INTO refeitorio (data, horaentrada, matricula, nome, serie, curso, refeicao) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        tuple(registro),
    )


def refeitorio_duplicado_db(matricula, refeicao):
    try:
        rows = executar_pg(
            "SELECT horaentrada, nome FROM refeitorio WHERE data = %s AND matricula = %s AND refeicao = %s LIMIT 1",
            (hoje(), matricula, refeicao),
            fetch=True,
        )
        if not rows:
            return None
        total_hoje = executar_pg(
            "SELECT COUNT(*) AS total FROM refeitorio WHERE data = %s",
            (hoje(),),
            fetch=True,
        )
        total_refeicao = executar_pg(
            "SELECT COUNT(*) AS total FROM refeitorio WHERE data = %s AND refeicao = %s",
            (hoje(), refeicao),
            fetch=True,
        )
        return {
            "hora": rows[0]["horaentrada"],
            "nome": rows[0]["nome"],
            "total_hoje": total_hoje[0].get("total", 0) if total_hoje else 0,
            "total_refeicao": total_refeicao[0].get("total", 0) if total_refeicao else 0,
        }
    except Exception as e:
        logger.warning(f"Duplicidade refeitorio: {e}")
        return None


def ler_frequencia_hoje_db():
    rows = executar_pg(
        "SELECT data, horaentrada, matricula, nome, serie, curso, aula FROM frequencia WHERE data = %s",
        (hoje(),),
        fetch=True,
    )
    if not rows:
        return []
    return [
        [row["data"], row["horaentrada"], row["matricula"], row["nome"], row["serie"], row["curso"], row["aula"]]
        for row in rows
    ]


def inserir_frequencia_db(registro):
    executar_pg(
        "INSERT INTO frequencia (data, horaentrada, matricula, nome, serie, curso, aula) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        tuple(registro),
    )


def frequencia_duplicado_db(matricula):
    try:
        rows = executar_pg(
            "SELECT horaentrada, nome FROM frequencia WHERE data = %s AND matricula = %s LIMIT 1",
            (hoje(), matricula),
            fetch=True,
        )
        if not rows:
            return None
        total_hoje = executar_pg(
            "SELECT COUNT(*) AS total FROM frequencia WHERE data = %s",
            (hoje(),),
            fetch=True,
        )
        return {
            "hora": rows[0]["horaentrada"],
            "nome": rows[0]["nome"],
            "total_hoje": total_hoje[0].get("total", 0) if total_hoje else 0,
        }
    except Exception as e:
        logger.warning(f"Duplicidade frequencia: {e}")
        return None


def inserir_avaliacao_db(registro):
    executar_pg(
        "INSERT INTO avaliacoes (data, aluno, serie, curso, estagio, item, nota) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        tuple(registro),
    )


def avaliacao_ja_existe_db(nome, semana_iso, ano_iso, serie=None, curso=None):
    """Verifica se o aluno já avaliou nesta semana ISO.

    Compara nome + série + curso (não só o nome) para evitar que dois
    alunos homônimos (nome igual, turma diferente) se bloqueiem
    mutuamente — situação comum em escolas grandes.

    A coluna `data` é armazenada como texto "DD/MM/AAAA". Comparar essas
    strings diretamente com BETWEEN (ordem alfabética) dá resultado errado
    sempre que a semana atravessa uma virada de mês — ex.: "01/02/2026"
    vem alfabeticamente ANTES de "28/01/2026", mesmo sendo uma data
    posterior. Por isso convertemos para DATE de verdade com TO_DATE antes
    de comparar. O filtro `data ~ '^\\d{2}/\\d{2}/\\d{4}$'` evita que uma
    linha com data mal formatada quebre a consulta inteira.
    """
    if not nome:
        return False
    data_inicio = datetime.date.fromisocalendar(ano_iso, semana_iso, 1)
    data_fim = data_inicio + datetime.timedelta(days=6)
    try:
        rows = executar_pg(
            """
            SELECT 1
            FROM avaliacoes
            WHERE lower(aluno) = lower(%s)
              AND lower(COALESCE(serie, '')) = lower(COALESCE(%s, ''))
              AND lower(COALESCE(curso, '')) = lower(COALESCE(%s, ''))
              AND data ~ '^\\d{2}/\\d{2}/\\d{4}$'
              AND TO_DATE(data, 'DD/MM/YYYY') BETWEEN %s AND %s
            LIMIT 1
            """,
            (nome, serie, curso, data_inicio, data_fim),
            fetch=True,
        )
    except Exception:
        return False
    return bool(rows)


def ler_refeitorio_periodo_db(data_inicio, data_fim, serie=None, curso=None):
    """Retorna registros de refeitório entre duas datas (inclusive), já
    filtrados no banco — em vez de trazer a tabela inteira e filtrar em
    Python (era o que a aba Histórico fazia antes; ficava mais lento a
    cada mês que passava).

    data_inicio / data_fim: objetos datetime.date.
    serie / curso: opcional, filtro parcial (contém), igual ao filtro que
    já existia na tela.
    """
    sql = """
        SELECT data, horaentrada, matricula, nome, serie, curso, refeicao
        FROM refeitorio
        WHERE data ~ '^\\d{2}/\\d{2}/\\d{4}$'
          AND TO_DATE(data, 'DD/MM/YYYY') BETWEEN %s AND %s
    """
    params = [data_inicio, data_fim]
    if serie:
        sql += " AND serie ILIKE %s"
        params.append(f"%{serie}%")
    if curso:
        sql += " AND curso ILIKE %s"
        params.append(f"%{curso}%")
    rows = executar_pg(sql, tuple(params), fetch=True)
    return [
        [row["data"], row["horaentrada"], row["matricula"], row["nome"], row["serie"], row["curso"], row["refeicao"]]
        for row in rows
    ]


def ler_frequencia_periodo_db(data_inicio, data_fim, serie=None, curso=None):
    """Mesma ideia de ler_refeitorio_periodo_db, para a tabela frequencia."""
    sql = """
        SELECT data, horaentrada, matricula, nome, serie, curso, aula
        FROM frequencia
        WHERE data ~ '^\\d{2}/\\d{2}/\\d{4}$'
          AND TO_DATE(data, 'DD/MM/YYYY') BETWEEN %s AND %s
    """
    params = [data_inicio, data_fim]
    if serie:
        sql += " AND serie ILIKE %s"
        params.append(f"%{serie}%")
    if curso:
        sql += " AND curso ILIKE %s"
        params.append(f"%{curso}%")
    rows = executar_pg(sql, tuple(params), fetch=True)
    return [
        [row["data"], row["horaentrada"], row["matricula"], row["nome"], row["serie"], row["curso"], row["aula"]]
        for row in rows
    ]


# ── Classificação de nota (Bom/Médio/Ruim) ──────────────────────────────────
# Antes essa mesma regra (nota>=4 → Bom, nota>=2 → Médio, senão Ruim)
# estava copiada em 3 arquivos diferentes do painel (pagina_avaliacoes.py,
# pagina_relatorio_ensino.py, pagina_relatorio_semanal.py). Hoje os três
# concordam entre si, mas cada um podia divergir silenciosamente se algum
# dia alguém mudasse o critério em só um lugar. Centralizando aqui.
def classificar_nota(nota):
    """Recebe uma nota (int/float/str numérica) de 1 a 5 e retorna
    'Bom', 'Medio' ou 'Ruim'. Retorna None se a nota não for numérica."""
    try:
        n = float(str(nota).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if n >= 4:
        return "Bom"
    if n >= 2:
        return "Medio"
    return "Ruim"


# ── Timestamp da última atualização (para polling do painel/index) ─────────
# Persistido no Neon (tabela sistema_config) em vez de variável em memória:
# assim funciona corretamente mesmo se a API reiniciar ou rodar com mais de
# um worker no futuro.
def ler_ultimo_update_ts():
    valor = ler_config_kv("ultimo_update_ts", {"ts": None})
    if isinstance(valor, dict):
        return valor.get("ts")
    return None


def marcar_ultimo_update_ts():
    ts = datetime.datetime.now().isoformat()
    try:
        salvar_config_kv("ultimo_update_ts", {"ts": ts})
    except Exception as e:
        logger.warning(f"Falha ao persistir ultimo_update_ts: {e}")
    return ts

# ── Respostas da gestão às avaliações ───────────────────────────────────────
SETORES_AVALIACAO = ["Geral", "Comida", "Limpeza", "Ensino", "Acolhimento"]


def inserir_resposta_gestao_db(setor, titulo, mensagem, autor=None):
    setor = setor if setor in SETORES_AVALIACAO else "Geral"
    criado_em = hoje() + " " + datetime.datetime.now().strftime("%H:%M")
    executar_pg(
        "INSERT INTO respostas_gestao (criado_em, setor, titulo, mensagem, autor) "
        "VALUES (%s, %s, %s, %s, %s)",
        (criado_em, setor, titulo, mensagem, autor),
    )


def ler_respostas_gestao_db(limite=20):
    rows = executar_pg(
        "SELECT id, criado_em, setor, titulo, mensagem, autor FROM respostas_gestao "
        "ORDER BY id DESC LIMIT %s",
        (limite,),
        fetch=True,
    )
    return rows or []


def apagar_resposta_gestao_db(resposta_id):
    executar_pg("DELETE FROM respostas_gestao WHERE id = %s", (resposta_id,))
