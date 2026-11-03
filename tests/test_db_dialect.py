"""受控方言翻译器：用代码库中的真实语句形状验证 PG 改写（plans §5）。"""

from __future__ import annotations

import pytest

from mindex.shared.db.dialect import (
    UnsupportedSql,
    convert_placeholders,
    quote_reserved,
    rewrite_for_postgres,
)


def test_placeholders_skip_strings_and_comments():
    sql = "SELECT ? AS a, '文?字' AS b, \"col?\" AS c FROM t WHERE x=? -- 注释 ?\nAND y=? /* 块 ? */"
    assert convert_placeholders(sql) == (
        "SELECT %s AS a, '文?字' AS b, \"col?\" AS c FROM t WHERE x=%s -- 注释 ?\nAND y=%s /* 块 ? */"
    )


def test_quote_reserved_end_and_trigger():
    assert quote_reserved('SELECT start, end FROM segments WHERE end > ?') == (
        'SELECT start, "end" FROM segments WHERE "end" > ?')
    assert quote_reserved("SELECT trigger FROM topic_runs") == 'SELECT "trigger" FROM topic_runs'
    assert quote_reserved("SELECT 'end' FROM t") == "SELECT 'end' FROM t"  # 字符串不动


def test_replace_rewrite_resets_defaults_and_bumps_rowid():
    sql = "INSERT OR REPLACE INTO representations(id,index_id,segment_id,view,document) VALUES(?,?,?,?,?)"
    assert rewrite_for_postgres(sql) == (
        "INSERT INTO representations(id,index_id,segment_id,view,document) VALUES(%s,%s,%s,%s,%s)"
        " ON CONFLICT (id) DO UPDATE SET id=excluded.id, index_id=excluded.index_id,"
        " segment_id=excluded.segment_id, view=excluded.view, document=excluded.document,"
        " cache_key=DEFAULT, rowid=DEFAULT"
    )


def test_replace_rewrite_without_column_list_uses_table_order():
    sql = "INSERT OR REPLACE INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)"
    rewritten = rewrite_for_postgres(sql)
    # 无列名：按表序补出列清单（保留字加引号），未列出的 rowid 重置
    assert rewritten.startswith(
        'INSERT INTO person_evidence(id,source_id,shot_id,actor_id,role_id,stage_id,start,"end",status,data)')
    assert "ON CONFLICT (id) DO UPDATE SET" in rewritten
    assert "rowid=DEFAULT" in rewritten
    assert '"end"=excluded."end"' in rewritten


def test_replace_rewrite_composite_conflict_key():
    sql = "INSERT OR REPLACE INTO channel_data(source_id,channel,profile,status,data) VALUES(?,'dialogue',?,?)"
    assert rewrite_for_postgres(sql) == (
        "INSERT INTO channel_data(source_id,channel,profile,status,data) VALUES(%s,'dialogue',%s,%s)"
        " ON CONFLICT (source_id, channel) DO UPDATE SET source_id=excluded.source_id,"
        " channel=excluded.channel, profile=excluded.profile, status=excluded.status,"
        " data=excluded.data, updated_at=DEFAULT, rowid=DEFAULT"
    )


def test_replace_rewrite_without_rowid_for_autoincrement_tables():
    sql = "INSERT OR REPLACE INTO model_profiles(id,role,name,provider,config) VALUES(?,?,?,?,?)"
    rewritten = rewrite_for_postgres(sql)
    assert "ON CONFLICT (id) DO UPDATE SET" in rewritten
    assert "rowid=DEFAULT" not in rewritten  # model_profiles 的 rowid 即 id
    assert "created_at=DEFAULT" in rewritten and "updated_at=DEFAULT" in rewritten


def test_unregistered_replace_table_is_rejected():
    with pytest.raises(UnsupportedSql):
        rewrite_for_postgres("INSERT OR REPLACE INTO unknown_table(a) VALUES(?)")


def test_insert_or_ignore_becomes_do_nothing():
    sql = "INSERT OR IGNORE INTO index_members(index_id,segment_id,snapshot) VALUES(?,?,?)"
    assert rewrite_for_postgres(sql) == (
        "INSERT INTO index_members(index_id,segment_id,snapshot) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING"
    )


def test_native_on_conflict_upsert_passes_through():
    sql = ("INSERT INTO media_groups(id,type,name,description) VALUES(?,?,?,?) "
           "ON CONFLICT(id) DO UPDATE SET description=excluded.description")
    assert rewrite_for_postgres(sql) == sql.replace("?", "%s")


def test_json_extract_text_equality_and_json_type():
    sql = ("SELECT a.segment_id,a.data FROM annotations a JOIN segments s ON s.id=a.segment_id "
           "WHERE s.source_id=? AND json_extract(a.data,'$.status')='skipped' "
           "AND (json_type(a.data,'$.annotation')='object' OR json_extract(a.data,'$.status')=?) "
           "AND a.rowid=(SELECT MAX(b.rowid) FROM annotations b WHERE b.segment_id=a.segment_id)")
    assert rewrite_for_postgres(sql) == (
        "SELECT a.segment_id,a.data FROM annotations a JOIN segments s ON s.id=a.segment_id "
        "WHERE s.source_id=%s AND (a.data::jsonb->>'status')='skipped' "
        "AND (jsonb_typeof(a.data::jsonb->'annotation')='object' OR (a.data::jsonb->>'status')=%s) "
        "AND a.rowid=(SELECT MAX(b.rowid) FROM annotations b WHERE b.segment_id=a.segment_id)"
    )


def test_json_extract_numeric_and_select_list_left_untouched():
    # 数值比较与 SELECT 列形状不匹配文本等值规则 → 原文（PG 上显式失败，不静默改写）
    numeric = "SELECT 1 FROM person_evidence WHERE json_extract(data,'$.shot_excluded')=1"
    assert "json_extract(data,'$.shot_excluded')" in rewrite_for_postgres(numeric)
    select_list = "SELECT DISTINCT json_extract(snapshot,'$.source_id') FROM index_members WHERE index_id=?"
    assert "json_extract(snapshot,'$.source_id')" in rewrite_for_postgres(select_list)


def test_case_end_terminator_is_not_quoted():
    sql = ("SELECT (CASE WHEN COALESCE(c.hidden,0)<>0 OR COALESCE(sc.hidden,0)<>0 THEN 1 ELSE 0 END) "
           "AS effective_hidden, g.end FROM segments g")
    assert quote_reserved(sql) == (
        'SELECT (CASE WHEN COALESCE(c.hidden,0)<>0 OR COALESCE(sc.hidden,0)<>0 THEN 1 ELSE 0 END) '
        'AS effective_hidden, g."end" FROM segments g')


def test_positional_insert_expands_columns():
    sql = "INSERT INTO segments VALUES('a','s','shot',0,1,'p','t','g',NULL,'r')"
    assert rewrite_for_postgres(sql) == (
        'INSERT INTO segments (id,source_id,kind,start,"end",path,thumbnail,generation,parent_id,reason) '
        "VALUES('a','s','shot',0,1,'p','t','g',NULL,'r')")


def test_rowid_ordering_stays_verbatim():
    sql = "SELECT id FROM search_releases WHERE content_id=? ORDER BY rowid DESC LIMIT 1"
    assert rewrite_for_postgres(sql) == "SELECT id FROM search_releases WHERE content_id=%s ORDER BY rowid DESC LIMIT 1"
