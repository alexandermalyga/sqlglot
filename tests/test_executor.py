import unittest
from datetime import date

import duckdb
import pandas as pd
from pandas.testing import assert_frame_equal

from sqlglot import exp, parse_one
from sqlglot.errors import ExecuteError
from sqlglot.executor import execute
from sqlglot.executor.python import Python
from sqlglot.executor.table import Table, ensure_tables
from tests.helpers import (
    FIXTURES_DIR,
    SKIP_INTEGRATION,
    TPCH_SCHEMA,
    load_sql_fixture_pairs,
)

DIR = FIXTURES_DIR + "/optimizer/tpc-h/"


@unittest.skipIf(SKIP_INTEGRATION, "Skipping Integration Tests since `SKIP_INTEGRATION` is set")
class TestExecutor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = duckdb.connect()

        for table, columns in TPCH_SCHEMA.items():
            cls.conn.execute(
                f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{DIR}{table}.csv.gz', delim='|', header=True, columns={columns})
                """
            )

        cls.cache = {}
        cls.sqls = [
            (sql, expected)
            for _, sql, expected in load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql")
        ]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def cached_execute(self, sql):
        if sql not in self.cache:
            self.cache[sql] = self.conn.execute(sql).fetchdf()
        return self.cache[sql]

    def rename_anonymous(self, source, target):
        for i, column in enumerate(source.columns):
            if "_col_" in column:
                source.rename(columns={column: target.columns[i]}, inplace=True)

    def test_py_dialect(self):
        self.assertEqual(Python().generate(parse_one("'x '''")), r"'x \''")

    def test_optimized_tpch(self):
        for i, (sql, optimized) in enumerate(self.sqls[:20], start=1):
            with self.subTest(f"{i}, {sql}"):
                a = self.cached_execute(sql)
                b = self.conn.execute(optimized).fetchdf()
                self.rename_anonymous(b, a)
                assert_frame_equal(a, b)

    def test_execute_tpch(self):
        def to_csv(expression):
            if isinstance(expression, exp.Table) and expression.name not in ("revenue"):
                return parse_one(
                    f"READ_CSV('{DIR}{expression.name}.csv.gz', 'delimiter', '|') AS {expression.alias_or_name}"
                )
            return expression

        for i, (sql, _) in enumerate(self.sqls):
            with self.subTest(f"tpch-h {i + 1}"):
                a = self.cached_execute(sql)
                sql = parse_one(sql).transform(to_csv).sql(pretty=True)
                table = execute(sql, TPCH_SCHEMA)
                b = pd.DataFrame(table.rows, columns=table.columns)
                assert_frame_equal(a, b, check_dtype=False, check_index_type=False)

    def test_execute_callable(self):
        tables = {
            "x": [
                {"a": "a", "b": "d"},
                {"a": "b", "b": "e"},
                {"a": "c", "b": "f"},
            ],
            "y": [
                {"b": "d", "c": "g"},
                {"b": "e", "c": "h"},
                {"b": "f", "c": "i"},
            ],
            "z": [],
        }
        schema = {
            "x": {
                "a": "VARCHAR",
                "b": "VARCHAR",
            },
            "y": {
                "b": "VARCHAR",
                "c": "VARCHAR",
            },
            "z": {"d": "VARCHAR"},
        }

        for sql, cols, rows in [
            ("SELECT * FROM x", ["a", "b"], [("a", "d"), ("b", "e"), ("c", "f")]),
            (
                "SELECT * FROM x JOIN y ON x.b = y.b",
                ["a", "b", "b", "c"],
                [("a", "d", "d", "g"), ("b", "e", "e", "h"), ("c", "f", "f", "i")],
            ),
            (
                "SELECT j.c AS d FROM x AS i JOIN y AS j ON i.b = j.b",
                ["d"],
                [("g",), ("h",), ("i",)],
            ),
            (
                "SELECT CONCAT(x.a, y.c) FROM x JOIN y ON x.b = y.b WHERE y.b = 'e'",
                ["_col_0"],
                [("bh",)],
            ),
            (
                "SELECT * FROM x JOIN y ON x.b = y.b WHERE y.b = 'e'",
                ["a", "b", "b", "c"],
                [("b", "e", "e", "h")],
            ),
            (
                "SELECT * FROM z",
                ["d"],
                [],
            ),
            (
                "SELECT d FROM z ORDER BY d",
                ["d"],
                [],
            ),
            (
                "SELECT a FROM x WHERE x.a <> 'b'",
                ["a"],
                [("a",), ("c",)],
            ),
            (
                "SELECT a AS i FROM x ORDER BY a",
                ["i"],
                [("a",), ("b",), ("c",)],
            ),
            (
                "SELECT a AS i FROM x ORDER BY i",
                ["i"],
                [("a",), ("b",), ("c",)],
            ),
            (
                "SELECT 100 - ORD(a) AS a, a AS i FROM x ORDER BY a",
                ["a", "i"],
                [(1, "c"), (2, "b"), (3, "a")],
            ),
            (
                "SELECT a /* test */ FROM x LIMIT 1",
                ["a"],
                [("a",)],
            ),
            (
                "SELECT DISTINCT a FROM (SELECT 1 AS a UNION ALL SELECT 1 AS a)",
                ["a"],
                [(1,)],
            ),
            (
                "SELECT DISTINCT a, SUM(b) AS b "
                "FROM (SELECT 'a' AS a, 1 AS b UNION ALL SELECT 'a' AS a, 2 AS b UNION ALL SELECT 'b' AS a, 1 AS b) "
                "GROUP BY a "
                "LIMIT 1",
                ["a", "b"],
                [("a", 3)],
            ),
            (
                "SELECT COUNT(1) AS a FROM (SELECT 1)",
                ["a"],
                [(1,)],
            ),
            (
                "SELECT COUNT(1) AS a FROM (SELECT 1) LIMIT 0",
                ["a"],
                [],
            ),
            (
                "SELECT a FROM x GROUP BY a LIMIT 0",
                ["a"],
                [],
            ),
            (
                "SELECT a FROM x LIMIT 0",
                ["a"],
                [],
            ),
        ]:
            with self.subTest(sql):
                result = execute(sql, schema=schema, tables=tables)
                self.assertEqual(result.columns, tuple(cols))
                self.assertEqual(result.rows, rows)

    def test_set_operations(self):
        tables = {
            "x": [
                {"a": "a"},
                {"a": "b"},
                {"a": "c"},
            ],
            "y": [
                {"a": "b"},
                {"a": "c"},
                {"a": "d"},
            ],
        }
        schema = {
            "x": {
                "a": "VARCHAR",
            },
            "y": {
                "a": "VARCHAR",
            },
        }

        for sql, cols, rows in [
            (
                "SELECT a FROM x UNION ALL SELECT a FROM y",
                ["a"],
                [("a",), ("b",), ("c",), ("b",), ("c",), ("d",)],
            ),
            (
                "SELECT a FROM x UNION SELECT a FROM y",
                ["a"],
                [("a",), ("b",), ("c",), ("d",)],
            ),
            (
                "SELECT a FROM x EXCEPT SELECT a FROM y",
                ["a"],
                [("a",)],
            ),
            (
                "SELECT a FROM x INTERSECT SELECT a FROM y",
                ["a"],
                [("b",), ("c",)],
            ),
            (
                """SELECT i.a
                FROM (
                  SELECT a FROM x UNION SELECT a FROM y
                ) AS i
                JOIN (
                  SELECT a FROM x UNION SELECT a FROM y
                ) AS j
                  ON i.a = j.a""",
                ["a"],
                [("a",), ("b",), ("c",), ("d",)],
            ),
            (
                "SELECT 1 AS a UNION SELECT 2 AS a UNION SELECT 3 AS a",
                ["a"],
                [(1,), (2,), (3,)],
            ),
        ]:
            with self.subTest(sql):
                result = execute(sql, schema=schema, tables=tables)
                self.assertEqual(result.columns, tuple(cols))
                self.assertEqual(set(result.rows), set(rows))

    def test_execute_catalog_db_table(self):
        tables = {
            "catalog": {
                "db": {
                    "x": [
                        {"a": "a"},
                        {"a": "b"},
                        {"a": "c"},
                    ],
                }
            }
        }
        schema = {
            "catalog": {
                "db": {
                    "x": {
                        "a": "VARCHAR",
                    }
                }
            }
        }
        result1 = execute("SELECT * FROM x", schema=schema, tables=tables)
        result2 = execute("SELECT * FROM catalog.db.x", schema=schema, tables=tables)
        assert result1.columns == result2.columns
        assert result1.rows == result2.rows

    def test_execute_tables(self):
        tables = {
            "sushi": [
                {"id": 1, "price": 1.0},
                {"id": 2, "price": 2.0},
                {"id": 3, "price": 3.0},
            ],
            "order_items": [
                {"sushi_id": 1, "order_id": 1},
                {"sushi_id": 1, "order_id": 1},
                {"sushi_id": 2, "order_id": 1},
                {"sushi_id": 3, "order_id": 2},
            ],
            "orders": [
                {"id": 1, "user_id": 1},
                {"id": 2, "user_id": 2},
            ],
        }

        self.assertEqual(
            execute(
                """
            SELECT
              o.user_id,
              SUM(s.price) AS price
            FROM orders o
            JOIN order_items i
              ON o.id = i.order_id
            JOIN sushi s
              ON i.sushi_id = s.id
            GROUP BY o.user_id
        """,
                tables=tables,
            ).rows,
            [
                (1, 4.0),
                (2, 3.0),
            ],
        )

        self.assertEqual(
            execute(
                """
            SELECT
              o.id, x.*
            FROM orders o
            LEFT JOIN (
                SELECT
                  1 AS id, 'b' AS x
                UNION ALL
                SELECT
                  3 AS id, 'c' AS x
            ) x
              ON o.id = x.id
        """,
                tables=tables,
            ).rows,
            [(1, 1, "b"), (2, None, None)],
        )
        self.assertEqual(
            execute(
                """
            SELECT
              o.id, x.*
            FROM orders o
            RIGHT JOIN (
                SELECT
                  1 AS id,
                  'b' AS x
                UNION ALL
                SELECT
                  3 AS id, 'c' AS x
            ) x
              ON o.id = x.id
        """,
                tables=tables,
            ).rows,
            [
                (1, 1, "b"),
                (None, 3, "c"),
            ],
        )

    def test_execute_subqueries(self):
        tables = {
            "table": [
                {"a": 1, "b": 1},
                {"a": 2, "b": 2},
            ],
        }

        self.assertEqual(
            execute(
                """
            SELECT *
            FROM table
            WHERE a = (SELECT MAX(a) FROM table)
        """,
                tables=tables,
            ).rows,
            [
                (2, 2),
            ],
        )

    def test_table_depth_mismatch(self):
        tables = {"table": []}
        schema = {"db": {"table": {"col": "VARCHAR"}}}
        with self.assertRaises(ExecuteError):
            execute("SELECT * FROM table", schema=schema, tables=tables)

    def test_tables(self):
        tables = ensure_tables(
            {
                "catalog1": {
                    "db1": {
                        "t1": [
                            {"a": 1},
                        ],
                        "t2": [
                            {"a": 1},
                        ],
                    },
                    "db2": {
                        "t3": [
                            {"a": 1},
                        ],
                        "t4": [
                            {"a": 1},
                        ],
                    },
                },
                "catalog2": {
                    "db3": {
                        "t5": Table(columns=("a",), rows=[(1,)]),
                        "t6": Table(columns=("a",), rows=[(1,)]),
                    },
                    "db4": {
                        "t7": Table(columns=("a",), rows=[(1,)]),
                        "t8": Table(columns=("a",), rows=[(1,)]),
                    },
                },
            }
        )

        t1 = tables.find(exp.table_(table="t1", db="db1", catalog="catalog1"))
        self.assertEqual(t1.columns, ("a",))
        self.assertEqual(t1.rows, [(1,)])

        t8 = tables.find(exp.table_(table="t8"))
        self.assertEqual(t1.columns, t8.columns)
        self.assertEqual(t1.rows, t8.rows)

    def test_static_queries(self):
        for sql, cols, rows in [
            ("SELECT 1", ["_col_0"], [(1,)]),
            ("SELECT 1 + 2 AS x", ["x"], [(3,)]),
            ("SELECT CONCAT('a', 'b') AS x", ["x"], [("ab",)]),
            ("SELECT 1 AS x, 2 AS y", ["x", "y"], [(1, 2)]),
            ("SELECT 'foo' LIMIT 1", ["_col_0"], [("foo",)]),
            (
                "SELECT SUM(x), COUNT(x) FROM (SELECT 1 AS x WHERE FALSE)",
                ["_col_0", "_col_1"],
                [(None, 0)],
            ),
        ]:
            with self.subTest(sql):
                result = execute(sql)
                self.assertEqual(result.columns, tuple(cols))
                self.assertEqual(result.rows, rows)

    def test_aggregate_without_group_by(self):
        result = execute("SELECT SUM(x) FROM t", tables={"t": [{"x": 1}, {"x": 2}]})
        self.assertEqual(result.columns, ("_col_0",))
        self.assertEqual(result.rows, [(3,)])

    def test_scalar_functions(self):
        for sql, expected in [
            ("CONCAT('a', 'b')", "ab"),
            ("CONCAT('a', NULL)", None),
            ("CONCAT_WS('_', 'a', 'b')", "a_b"),
            ("STR_POSITION('bar', 'foobarbar')", 4),
            ("STR_POSITION('bar', 'foobarbar', 5)", 7),
            ("STR_POSITION(NULL, 'foobarbar')", None),
            ("STR_POSITION('bar', NULL)", None),
            ("UPPER('foo')", "FOO"),
            ("UPPER(NULL)", None),
            ("LOWER('FOO')", "foo"),
            ("LOWER(NULL)", None),
            ("IFNULL('a', 'b')", "a"),
            ("IFNULL(NULL, 'b')", "b"),
            ("IFNULL(NULL, NULL)", None),
            ("SUBSTRING('12345')", "12345"),
            ("SUBSTRING('12345', 3)", "345"),
            ("SUBSTRING('12345', 3, 0)", ""),
            ("SUBSTRING('12345', 3, 1)", "3"),
            ("SUBSTRING('12345', 3, 2)", "34"),
            ("SUBSTRING('12345', 3, 3)", "345"),
            ("SUBSTRING('12345', 3, 4)", "345"),
            ("SUBSTRING('12345', -3)", "345"),
            ("SUBSTRING('12345', -3, 0)", ""),
            ("SUBSTRING('12345', -3, 1)", "3"),
            ("SUBSTRING('12345', -3, 2)", "34"),
            ("SUBSTRING('12345', 0)", ""),
            ("SUBSTRING('12345', 0, 1)", ""),
            ("SUBSTRING(NULL)", None),
            ("SUBSTRING(NULL, 1)", None),
            ("CAST(1 AS TEXT)", "1"),
            ("CAST('1' AS LONG)", 1),
            ("CAST('1.1' AS FLOAT)", 1.1),
            ("COALESCE(NULL)", None),
            ("COALESCE(NULL, NULL)", None),
            ("COALESCE(NULL, 'b')", "b"),
            ("COALESCE('a', 'b')", "a"),
            ("1 << 1", 2),
            ("1 >> 1", 0),
            ("1 & 1", 1),
            ("1 | 1", 1),
            ("1 < 1", False),
            ("1 <= 1", True),
            ("1 > 1", False),
            ("1 >= 1", True),
            ("1 + NULL", None),
            ("IF(true, 1, 0)", 1),
            ("IF(false, 1, 0)", 0),
            ("CASE WHEN 0 = 1 THEN 'foo' ELSE 'bar' END", "bar"),
            ("CAST('2022-01-01' AS DATE) + INTERVAL '1' DAY", date(2022, 1, 2)),
            ("1 IN (1, 2, 3)", True),
            ("1 IN (2, 3)", False),
            ("NULL IS NULL", True),
            ("NULL IS NOT NULL", False),
            ("NULL = NULL", None),
            ("NULL <> NULL", None),
        ]:
            with self.subTest(sql):
                result = execute(f"SELECT {sql}")
                self.assertEqual(result.rows, [(expected,)])

    def test_case_sensitivity(self):
        result = execute("SELECT A AS A FROM X", tables={"x": [{"a": 1}]})
        self.assertEqual(result.columns, ("A",))
        self.assertEqual(result.rows, [(1,)])
