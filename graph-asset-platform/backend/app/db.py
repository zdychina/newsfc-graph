"""SQLite 持久化层：连接管理 + schema 初始化。

单文件 ``platform.db``，WAL 模式（读写并发），``foreign_keys=ON``（tests 级联删除）。
单连接 ``check_same_thread=False``，写事务由 ``service.import_lock`` / ``tests.test_lock``
保护（避免并发写触发 SQLite BUSY）。

迁移版本记在 ``meta.schema_version``，未来 schema 演进在此 bump + 加迁移逻辑。
"""
import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA_VERSION = "14"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects(
  id TEXT, version TEXT, type TEXT, layer TEXT, scope TEXT,
  nf TEXT, domain TEXT, scenario TEXT,
  source_path TEXT, name TEXT, frontmatter_json TEXT,
  body_md TEXT, raw_md TEXT, mtime REAL,
  PRIMARY KEY(id, version)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_objects_nf ON objects(nf);
CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(type);
-- source_path 索引（v7，2026-08-25）：reindex_path 每文件 delete_by_source 原为
-- 全表扫（连带 body_md/raw_md 正文页）——批量增量索引 O(N²) 的主因之一
CREATE INDEX IF NOT EXISTS idx_objects_source ON objects(source_path);

CREATE TABLE IF NOT EXISTS edges(
  from_id TEXT, from_version TEXT, relation TEXT, "to" TEXT,
  PRIMARY KEY(from_id, from_version, relation, "to")
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges("to");

CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS import_jobs(
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  nf TEXT DEFAULT '', version TEXT DEFAULT '',
  status TEXT NOT NULL, added INTEGER DEFAULT 0,
  updated INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  steps TEXT DEFAULT '[]', result TEXT DEFAULT '{}', warnings TEXT DEFAULT '[]',
  error TEXT DEFAULT '', started_at REAL NOT NULL, finished_at REAL DEFAULT 0,
  child_pids TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_import_jobs_started ON import_jobs(started_at);

CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, key TEXT,
  can_frontend INT, can_assets INT, can_upload INT, can_test INT, can_skill INT, is_admin INT,
  created_at TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS trash(
  id TEXT PRIMARY KEY, original_path TEXT, is_dir INT,
  md_count INT, deleted_at TEXT, deleted_by TEXT
);

CREATE TABLE IF NOT EXISTS telemetry(
  ts TEXT, level TEXT, caller TEXT, endpoint TEXT,
  obj_id TEXT, obj_type TEXT, user TEXT, operator TEXT,
  session_id TEXT DEFAULT '', params TEXT DEFAULT '', result TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tel_stats ON telemetry(level, caller, endpoint, ts);
CREATE INDEX IF NOT EXISTS idx_tel_user ON telemetry(user, level, ts);

-- 正文全文索引（MCP search_md，CR：MCP 服务化 2026-08-24）。trigram 分词：
-- 中英文统一子串语义，且 LIKE/GLOB 可走 trigram 索引（<3 字符查询的回退路径）。
-- 独立表（objects 是 WITHOUT ROWID，external-content 方案不适用）。
CREATE VIRTUAL TABLE IF NOT EXISTS md_fts USING fts5(
  obj_id UNINDEXED, version UNINDEXED, body,
  tokenize='trigram'
);

-- md_fts 伴生映射（v7，2026-08-25）：(obj_id,version)→fts rowid。按 UNINDEXED 列
-- DELETE 是全 FTS 扫（含全部正文页）——批量 reindex 每文件一扫成 O(N²) 主因；
-- 改按 rowid 删。fts_repo 维护；init_schema 对存量行一次性回填（map 空且 fts
-- 非空时）。map 缺失时 fts_repo 回退全扫删（正确性网底）。
CREATE TABLE IF NOT EXISTS md_fts_map(
  obj_id TEXT NOT NULL, version TEXT NOT NULL, fts_rowid INTEGER NOT NULL,
  PRIMARY KEY(obj_id, version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS test_cases(
  id TEXT PRIMARY KEY, domain TEXT, scenario TEXT, name TEXT,
  status TEXT, solution TEXT, author TEXT, created_at TEXT,
  body_md TEXT, raw_md TEXT, source_path TEXT, frontmatter_json TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS test_runs(
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES test_cases(id) ON DELETE CASCADE,
  name TEXT, runner TEXT, run_at TEXT, status TEXT, latest_verdict TEXT,
  body_md TEXT, raw_md TEXT, source_path TEXT, frontmatter_json TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS test_reviews(
  id TEXT PRIMARY KEY, run_id TEXT REFERENCES test_runs(id) ON DELETE CASCADE,
  reviewer TEXT, reviewed_at TEXT, verdict TEXT,
  body_md TEXT, raw_md TEXT, source_path TEXT, frontmatter_json TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS test_review_problems(
  id INTEGER PRIMARY KEY, review_id TEXT REFERENCES test_reviews(id) ON DELETE CASCADE,
  idx INT, description TEXT, attribution_json TEXT, objects_json TEXT
);

CREATE TABLE IF NOT EXISTS test_artifacts(
  id INTEGER PRIMARY KEY, owner_type TEXT, owner_id TEXT,
  path TEXT, kind TEXT, size INT
);

-- MCP 工具配置（admin 前端可配，2026-08-25；v13 三态迁移 2026-09-08）：
-- visibility 三态（visible 展示+可调 / hidden 不展示+可调 / disabled 不展示+
-- TOOL_DISABLED）；description 物理列复用存 supplemental_description（API/UI
-- 改名，仅追加不覆盖 canonical）；enabled 列仅供旧数据迁移读取，新代码以
-- visibility 判定。服务总体说明存 meta 表 key='mcp_instructions'（v13 起语义=
-- 追加补充，canonical 在代码）。
CREATE TABLE IF NOT EXISTS mcp_tools(
  tool_name TEXT PRIMARY KEY,
  enabled INT NOT NULL DEFAULT 1,
  description TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'visible'
    CHECK(visibility IN ('visible','hidden','disabled'))
);
-- 抽取产物清单（v9，抽取任务化 2026-08-26）：入图闸门 confirm 后写入，按任务回退
-- （revert）的依据。op: add=本次新增（回退=软删进回收站）；modify=本次覆盖（回退=
-- 还原 originals 备份）。sha256=应用后内容摘要（回退前比对磁盘，防后续任务已覆盖）。
CREATE TABLE IF NOT EXISTS extract_files(
  job_id TEXT NOT NULL, path TEXT NOT NULL,
  op TEXT NOT NULL, sha256 TEXT NOT NULL,
  layer TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(job_id, path)
);
CREATE INDEX IF NOT EXISTS idx_extract_files_job ON extract_files(job_id);

CREATE INDEX IF NOT EXISTS idx_runs_case ON test_runs(case_id);
CREATE INDEX IF NOT EXISTS idx_reviews_run ON test_reviews(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner ON test_artifacts(owner_type, owner_id);

-- ============ AIMML 历史规则表（v11，统计页 2026-09-01）============
-- 8 张表由内网 GaussDB 全量导出灌入（AIMML历史图谱/db_tool/dump_rule_tables_to_platform.py），
-- 平台侧只读（统计页数据源），列名/结构与源库一致。DDL 与《图谱平台统计页面需求说明书》
-- §9 逐字一致——修改须先改需求文档。网元列名两套：GRAPH/REPEAT/LOGICAL_NE/映射表用
-- PHYSICAL_NE_TYPE；MOD/SET/DELETE/语法规则表用 NE_TYPE（stats/spec.py 维护映射）。
CREATE TABLE IF NOT EXISTS "B_AI_COMMAND_SYNTAX_CHECK_RULES" (
  "CMD_NAME"            TEXT,  -- 命令名，如 'SET OFI'、'ADD DIAMMEDACT'
  "PARAM_ID"            TEXT,  -- 参数 ID（每命令内自增序号）
  "PARAM_NAME"          TEXT,  -- 参数名
  "DATA_TYPE"           TEXT,  -- 数据类型
  "OPTIONAL_MANDATORY"  TEXT,  -- 参数属性：可选/必选/条件必选/条件可选
  "BIT_FIELD"           TEXT,  -- 位域标志
  "DEFAULTVALUE"        TEXT,  -- 默认值
  "MAX_VALUE"           TEXT,  -- 最大值
  "MIN_VALUE"           TEXT,  -- 最小值
  "INTERVAL_VALUE"      TEXT,  -- 步长/间隔
  "MAX_LENGTH"          TEXT,  -- 最大长度
  "MIN_LENGTH"          TEXT,  -- 最小长度
  "DATA_LENGTH"         TEXT,  -- 数据长度
  "ASSOCIATED_COMMANDS" TEXT,  -- 关联命令
  "ASSOCIATED_PARAMRS"  TEXT,  -- 关联参数
  "CASE_SENSITIVE"      TEXT,  -- 是否大小写敏感
  "FORMAT"              TEXT,  -- 格式（CLOB 原列，已转全文）
  "VALUE_RANGE"         TEXT,  -- 取值范围（CLOB 原列）
  "REG_ID"              TEXT,  -- 规则 ID
  "DESCRIPTION"         TEXT,  -- 描述（CLOB 原列）
  "NE_TYPE"             TEXT,  -- 网元类型（本表自有命名，如 'vSE2980_4U'/'UEG-M'）
  "NE_VERSION"          TEXT,  -- 版本，如 'V500R025C10'、'20.9.2'
  "PARAM_NAME_CN"       TEXT,  -- 参数中文名
  "COMPARERELATION"     TEXT,  -- 比较关系
  "FORBIDDINGINPUT"     TEXT,  -- 禁止输入
  "ENUMVALUE"           TEXT,  -- 枚举值（CLOB 原列）
  "CONDITION_RANGE"     TEXT,  -- 条件范围
  "CONDITION_SELECTION" TEXT,  -- 条件选择（CLOB 原列）
  "PARAM_NAME_EN"       TEXT,  -- 参数英文名
  "COMMAND_DESC_CN"     TEXT,  -- 命令中文描述（CLOB 原列）
  "COMMAND_DESC_EN"     TEXT,  -- 命令英文描述
  "NEW_PARAM_ID"        TEXT,  -- 新参数 ID（预留）
  "PARAM_PREFIX"        TEXT   -- 参数前缀（预留）
);
CREATE INDEX IF NOT EXISTS idx_syntax_ne ON "B_AI_COMMAND_SYNTAX_CHECK_RULES"("NE_TYPE","NE_VERSION");
CREATE INDEX IF NOT EXISTS idx_syntax_cmd ON "B_AI_COMMAND_SYNTAX_CHECK_RULES"("CMD_NAME","NE_TYPE","NE_VERSION");

CREATE TABLE IF NOT EXISTS "B_AI_CONFIG_CHECK_LOGICAL_NE_CMD_T" (
  "PHYSICAL_NE_TYPE" TEXT,  -- 物理网元，如 'UNC'
  "LOGICAL_NE_TYPE"  TEXT,  -- 逻辑网元，如 'SMSF'、'AMF'、'SMF'
  "NE_VERSION"       TEXT,  -- 版本，如 '23.1.0'
  "COMMAND_NAME"     TEXT   -- 命令名，如 'SET HSMFCOMMONSW'
);
CREATE INDEX IF NOT EXISTS idx_lne_ne ON "B_AI_CONFIG_CHECK_LOGICAL_NE_CMD_T"("PHYSICAL_NE_TYPE","LOGICAL_NE_TYPE","NE_VERSION");

CREATE TABLE IF NOT EXISTS "B_AI_MML_GRAPH_RULE_T" (
  "ID"                      TEXT,
  "PHYSICAL_NE_TYPE"        TEXT,
  "NE_VERSION"              TEXT,
  "COMMAND_NAME"            TEXT,
  "PARAM_NAME"              TEXT,
  "DEPEND_COMMAND_NAME"     TEXT,
  "DEPEND_PARAM_NAME"       TEXT,
  "DEPEND_TYPE"             TEXT,
  "CONDITION_COMMAND_NAME"  TEXT,
  "CONDITION_PARAM_NAME"    TEXT,
  "CONDITION_PARAM_VALUE"   TEXT,
  "MATCH_RULE_CODE"         TEXT,
  "RELATION_RULE_CODE"      TEXT,
  "ERR_MSG_ZH"              TEXT,
  "ERR_MSG_EN"              TEXT,
  "DEFAULT_ERROR_ZH"        TEXT,
  "DEFAULT_ERROR_EN"        TEXT,
  "BIND_TYPE"               TEXT,
  "LINKED_DELETE"           TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_ne ON "B_AI_MML_GRAPH_RULE_T"("PHYSICAL_NE_TYPE","NE_VERSION","COMMAND_NAME");

CREATE TABLE IF NOT EXISTS "B_AI_MML_REPEAT_CHECK_RULE_T" (
  "REPEAT_CHECK_RULE_ID" TEXT,
  "PHYSICAL_NE_TYPE"     TEXT,
  "NE_VERSION"           TEXT,
  "CREATE_TIME"          TEXT,
  "UPDATE_TIME"          TEXT,
  "COMMAND_NAME"         TEXT,
  "PARAMS"               TEXT,
  "RELATION"             TEXT,
  "ERROR_MSG_CN"         TEXT,
  "ERROR_MSG_EN"         TEXT
);
CREATE INDEX IF NOT EXISTS idx_repeat_ne ON "B_AI_MML_REPEAT_CHECK_RULE_T"("PHYSICAL_NE_TYPE","NE_VERSION","COMMAND_NAME");

CREATE TABLE IF NOT EXISTS "B_AI_MOD_RULE_T" (
  "ID"                            TEXT,
  "MOD_CMD"                       TEXT,
  "ADD_CMD"                       TEXT,
  "INDEX_PARAMS"                  TEXT,
  "COMMON_PARAMS"                 TEXT,
  "INDEPENDENT_EXISTENCE"         TEXT,
  "MOD_PARAM"                     TEXT,
  "ADD_PARAM"                     TEXT,
  "RESERVED"                      TEXT,
  "SPECIAL_ASSOCIATION_CONDITIONS" TEXT,
  "NE_VERSION"                    TEXT,
  "NE_TYPE"                       TEXT,
  "ERR_MSG_EN"                    TEXT,
  "ERR_MSG_CN"                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_mod_ne ON "B_AI_MOD_RULE_T"("NE_TYPE","NE_VERSION","MOD_CMD");

CREATE TABLE IF NOT EXISTS "B_AI_MML_SET_CHECK_RULE_T" (
  "NE_TYPE"            TEXT,
  "NE_VERSION"         TEXT,
  "CMD_NAME"           TEXT,
  "PARAM_ID"           TEXT,
  "PARAM_NAME"         TEXT,
  "DATA_TYPE"          TEXT,
  "KEY_PARAMETER"      TEXT,
  "COMMAND_UNIQUE"     TEXT,
  "OPTIONAL_MANDATORY" TEXT,
  "CONDITION_SELECTION" TEXT,
  "CASE_SENSITIVE"     TEXT
);
CREATE INDEX IF NOT EXISTS idx_set_ne ON "B_AI_MML_SET_CHECK_RULE_T"("NE_TYPE","NE_VERSION","CMD_NAME");

CREATE TABLE IF NOT EXISTS "B_AI_DELETE_RULE_V2_T" (
  "ID"                 TEXT,
  "EXCUTE_CMD"         TEXT,
  "EXCUTE_PARAMS"      TEXT,
  "RELATION_ADD_CMD"   TEXT,
  "RMV_SPECIAL"        TEXT,
  "FIND_RMV_JEXL_CODE" TEXT,
  "ERR_MSG_EN"         TEXT,
  "ERR_MSG_CN"         TEXT,
  "NE_TYPE"            TEXT,
  "NE_VERSION"         TEXT
);
CREATE INDEX IF NOT EXISTS idx_del_ne ON "B_AI_DELETE_RULE_V2_T"("NE_TYPE","NE_VERSION","EXCUTE_CMD");

CREATE TABLE IF NOT EXISTS "B_AI_NE_VERSION_MAPPING_T" (
  "UUID"             TEXT,
  "PHYSICAL_NE_TYPE" TEXT,
  "LOGICAL_NE_TYPE"  TEXT,
  "NE_VIEW"          TEXT,
  "LOCAL_VERSION"    TEXT,
  "OVERSEAS_VERSION" TEXT,
  "SUPPORT"          TEXT,
  "DOMAIN_NAME"      TEXT,
  "DOMAIN_NAME_EN"   TEXT
);
CREATE INDEX IF NOT EXISTS idx_vm_ne ON "B_AI_NE_VERSION_MAPPING_T"("PHYSICAL_NE_TYPE","LOCAL_VERSION");

-- ============ 统一搜索（v12，三工具重构 2026-09-08）============
-- 每 ID 最新版本物化（需求 §7.5：最新版过滤必须在 term 搜索前于 SQL 生效）。
-- version 沿用 objects 的 DB 表示（无版本=空串""，禁写 NULL）；刷新一律走
-- repos/object_latest_repo（Python latest_version 语义化比较，禁 SQL MAX）。
CREATE TABLE IF NOT EXISTS object_latest(
  id TEXT PRIMARY KEY,
  version TEXT NOT NULL
) WITHOUT ROWID;

-- 统一搜索 FTS（§7.3/§12.2）：metadata_text=规范化 id/name/name_zh 换行连接；
-- body_text=规范化正文（原始 body_md 留在 objects 供 snippet）。旧 md_fts 在
-- legacy 周期内保留给 search_md。trigram：中英统一子串语义 + LIKE 走索引。
CREATE VIRTUAL TABLE IF NOT EXISTS graph_search_fts USING fts5(
  obj_id UNINDEXED, version UNINDEXED,
  metadata_text, body_text,
  tokenize='trigram'
);

-- 伴生映射（同 md_fts_map v7 教训：按 UNINDEXED 列 DELETE 是全 FTS 扫，
-- 批量 reindex 每文件一扫成 O(N²)）——按 rowid O(1) 删。
CREATE TABLE IF NOT EXISTS graph_search_map(
  obj_id TEXT NOT NULL, version TEXT NOT NULL, fts_rowid INTEGER NOT NULL,
  PRIMARY KEY(obj_id, version)
) WITHOUT ROWID;

-- 搜索过滤索引（§12.2，EXPLAIN 驱动不过度堆索引）
CREATE INDEX IF NOT EXISTS idx_objects_nf_type_version ON objects(nf, type, version);
CREATE INDEX IF NOT EXISTS idx_objects_domain_scenario ON objects(domain, scenario);

-- ============ 文件户口册（v14，search_files 2026-09-29 spec §4.1）============
-- assets 下所有文件 + 目录行（目录行支撑 path 模式 ls 式直接子项浏览）。
-- files_fts.name 存规范化文件名（NFKC→strip→casefold，复用 graph_search_repo
-- 的 normalize_search_text）；files.name 存原样（响应展示）。伴生 map 同
-- md_fts_map/graph_search_map 教训：按 rowid O(1) 删，防批量维护 O(N²)。
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY,          -- 相对 assets 根，正斜杠，磁盘真实大小写
  name TEXT NOT NULL,
  ext  TEXT NOT NULL DEFAULT '',  -- 小写无点；目录恒 ''
  is_dir INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,   -- 目录恒 0
  mtime REAL NOT NULL DEFAULT 0      -- 目录不随子项变更刷新（仅展示，避免噪音）
) WITHOUT ROWID;

-- ext-only / ext+after 游标检索：等值列在前、path 范围与排序在后。
CREATE INDEX IF NOT EXISTS idx_files_ext_path ON files(ext, path);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  path UNINDEXED, name, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS files_fts_map(
  path TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL
) WITHOUT ROWID;
"""


def get_db(path: Path = None) -> sqlite3.Connection:
    """打开/创建 SQLite 连接（WAL + NORMAL + foreign_keys + Row 工厂）。

    synchronous=NORMAL（WAL 下）：commit 不再逐条 fsync（reindex 逐文件 commit 曾
    因此每小时多花几十秒）。代价仅 OS 崩溃/断电丢最近提交——图谱索引可由 md
    全量重建（mtime 对账/全量重建兜底），users/trash/jobs 等元数据丢最后一笔
    可接受；应用崩溃不丢（WAL 特性）。
    """
    conn = sqlite3.connect(str(path or DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表（IF NOT EXISTS）+ 记录 schema_version。幂等。"""
    conn.executescript(_SCHEMA)
    # v2 迁移：users.can_assets（资产目录权限）。旧库补列 + admin 回填
    # （check_perm 对 is_admin 短路全权，回填使 DB 位与实际效力一致）。幂等。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "can_assets" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN can_assets INT DEFAULT 0")
    conn.execute("UPDATE users SET can_assets=1 WHERE is_admin=1 AND can_assets=0")
    # v3 迁移：import_jobs.child_pids（jobs 独立连接记子进程 PID，sweep 终止孤儿用）。幂等。
    jcols = {r[1] for r in conn.execute("PRAGMA table_info(import_jobs)")}
    if jcols and "child_pids" not in jcols:
        conn.execute("ALTER TABLE import_jobs ADD COLUMN child_pids TEXT DEFAULT '[]'")
    # v10：ImportJob.updated/skipped 原只存内存，重启后丢失。
    if jcols and "updated" not in jcols:
        conn.execute("ALTER TABLE import_jobs ADD COLUMN updated INTEGER DEFAULT 0")
    if jcols and "skipped" not in jcols:
        conn.execute("ALTER TABLE import_jobs ADD COLUMN skipped INTEGER DEFAULT 0")
    # v4 迁移（MCP 服务化 2026-08-24）：① telemetry.session_id（会话ID 打点新列，
    # 历史行为 ''）② md_fts 存量回填（旧库 objects 有数据而 FTS 空表 → 一次性灌入）。
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)")}
    if tcols and "session_id" not in tcols:
        conn.execute("ALTER TABLE telemetry ADD COLUMN session_id TEXT DEFAULT ''")
    # v5 迁移（MCP 参数留痕 2026-08-24）：telemetry.params/result（tool 级行的
    # 入参 JSON + 出参摘要 JSON，截断 2KB；用户要求输入输出都记录）。
    if tcols and "params" not in tcols:
        conn.execute("ALTER TABLE telemetry ADD COLUMN params TEXT DEFAULT ''")
    if tcols and "result" not in tcols:
        conn.execute("ALTER TABLE telemetry ADD COLUMN result TEXT DEFAULT ''")
    if conn.execute("SELECT COUNT(*) FROM md_fts").fetchone()[0] == 0:
        n = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        if n:
            conn.execute(
                "INSERT INTO md_fts(obj_id, version, body) "
                "SELECT id, version, body_md FROM objects"
            )
    # v7 迁移：md_fts_map 存量回填（一次性——仅 map 空且 fts 非空；此后由
    # fts_repo 维护）。单遍扫描，老库首启多花秒级。
    if conn.execute("SELECT COUNT(*) FROM md_fts_map").fetchone()[0] == 0:
        n = conn.execute("SELECT COUNT(*) FROM md_fts").fetchone()[0]
        if n:
            conn.execute(
                "INSERT INTO md_fts_map(obj_id, version, fts_rowid) "
                "SELECT obj_id, version, rowid FROM md_fts"
            )
    # v8 迁移（打点瘦身 2026-08-26·方案B）：一次性清理历史 request 级行——
    # 任务面板轮询/浏览读请求曾占绝对大头且无统计价值（object/tool 级保留）。
    # 此后 request 级仅剩 fs/import 写操作审计行（各 router 自行 _record）。
    if not conn.execute(
        "SELECT value FROM meta WHERE key='telemetry_request_purged'"
    ).fetchone():
        conn.execute("DELETE FROM telemetry WHERE level='request'")
        conn.execute("INSERT INTO meta(key, value) VALUES('telemetry_request_purged','1')")
    # v12 迁移（三工具重构 2026-09-08）：① object_latest 一次性回填（Python
    # 语义化取最新，禁 SQL MAX）② graph_search_fts 一次性灌入（规范化文本）。
    # 用**完整性检查**（而非"表空才建"）判幂等：rebuild 分块提交中途崩溃留下的
    # 半灌入态也会被识别重灌（代码审查 MEDIUM）；完整性检查本身只做计数/行集
    # 对账，成本可忽略。此后由 service 写路径增量维护；启动对账兜底内容漂移。
    from .repos import graph_search_repo, object_latest_repo
    if conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]:
        if not object_latest_repo.integrity_ok(conn):
            object_latest_repo.rebuild(conn)
        if not graph_search_repo.integrity_ok(conn):
            graph_search_repo.rebuild_from_objects(conn)
    # v13 迁移（三工具重构 M3，一次性——哨兵防重，幂等）：① mcp_tools 加
    # visibility 三态列（存量 enabled=0→disabled，=1→visible）；② legacy 三工具
    # 设 hidden（可直调不出现在 tools/list）；③ 插入 search_graph=visible；
    # ④ meta.mcp_instructions 旧值（5 工具时代全文覆盖）备份到
    # mcp_instructions_legacy_backup 并清空 active——旧说明含已下线的 search_md
    # 引导，继续生效会误导；管理员可从备份把仍适用内容重新加入补充说明。
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(mcp_tools)")}
    if mcols and "visibility" not in mcols:
        conn.execute(
            "ALTER TABLE mcp_tools ADD COLUMN visibility TEXT NOT NULL "
            "DEFAULT 'visible' "
            "CHECK(visibility IN ('visible','hidden','disabled'))")
        conn.execute(
            "UPDATE mcp_tools SET visibility="
            "CASE WHEN enabled=0 THEN 'disabled' ELSE 'visible' END")
    if not conn.execute(
        "SELECT value FROM meta WHERE key='mcp_visibility_migrated'"
    ).fetchone():
        for tool in ("search_objects", "search_md", "get_object"):
            conn.execute(
                "INSERT INTO mcp_tools(tool_name, enabled, description, visibility) "
                "VALUES(?, 1, '', 'hidden') "
                "ON CONFLICT(tool_name) DO UPDATE SET visibility='hidden'",
                (tool,))
        conn.execute(
            "INSERT INTO mcp_tools(tool_name, enabled, description, visibility) "
            "VALUES('search_graph', 1, '', 'visible') "
            "ON CONFLICT(tool_name) DO NOTHING")
        old = conn.execute(
            "SELECT value FROM meta WHERE key='mcp_instructions'").fetchone()
        if old and old["value"]:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) "
                "VALUES('mcp_instructions_legacy_backup', ?)", (old["value"],))
        conn.execute(
            "UPDATE meta SET value='' WHERE key='mcp_instructions'")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('mcp_visibility_migrated','1')")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


_shared: "sqlite3.Connection | None" = None


def get_shared_db() -> sqlite3.Connection:
    """全局共享连接单例（service / users / store / telemetry 共用，避免多连接写冲突）。

    首次调用打开 ``DB_PATH`` 并 ``init_schema``；之后复用。测试通过 ``monkeypatch``
    把 ``db._shared`` 指向 tmp 连接来隔离。
    """
    global _shared
    if _shared is None:
        _shared = get_db()
        init_schema(_shared)
    return _shared
