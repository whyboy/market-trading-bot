"""每日结果表的数据访问。"""


class DailyResultsRepository:
    @staticmethod
    def create_table(connection, parameters=()):
        return connection.execute("CREATE TABLE IF NOT EXISTS daily_results (\n                day TEXT PRIMARY KEY, execution_payload TEXT, valuation_payload TEXT,\n                valuation_status TEXT NOT NULL DEFAULT 'pending', valuation_error TEXT NOT NULL DEFAULT '',\n                updated_at TEXT NOT NULL DEFAULT '')", parameters)

    @staticmethod
    def insert_execution(connection, parameters=()):
        return connection.execute("INSERT INTO daily_results(day, execution_payload) VALUES (?, ?)\n                ON CONFLICT(day) DO UPDATE SET execution_payload=excluded.execution_payload,\n                valuation_payload=NULL, valuation_status='pending', valuation_error=''\n                WHERE daily_results.execution_payload IS NULL", parameters)

    @staticmethod
    def save_valuation(connection, parameters=()):
        return connection.execute("INSERT INTO daily_results(day, valuation_payload, valuation_status, updated_at)\n                VALUES (?, ?, 'success', ?) ON CONFLICT(day) DO UPDATE SET\n                valuation_payload=excluded.valuation_payload, valuation_status='success',\n                valuation_error='', updated_at=excluded.updated_at", parameters)

    @staticmethod
    def set_valuation_status(connection, parameters=()):
        return connection.execute('INSERT INTO daily_results(day, valuation_status, valuation_error, updated_at)\n                VALUES (?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET valuation_status=excluded.valuation_status,\n                valuation_error=excluded.valuation_error, updated_at=excluded.updated_at', parameters)

    @staticmethod
    def latest_execution_before(connection, parameters=()):
        return connection.execute('SELECT execution_payload FROM daily_results WHERE day<=? AND execution_payload IS NOT NULL ORDER BY day DESC LIMIT 1', parameters)

    @staticmethod
    def execution_for_day(connection, parameters=()):
        return connection.execute('SELECT execution_payload FROM daily_results WHERE day = ?', parameters)

    @staticmethod
    def latest_execution(connection, parameters=()):
        return connection.execute('SELECT execution_payload FROM daily_results WHERE execution_payload IS NOT NULL ORDER BY day DESC LIMIT 1', parameters)

    @staticmethod
    def all_executions(connection, parameters=()):
        return connection.execute('SELECT execution_payload FROM daily_results WHERE execution_payload IS NOT NULL ORDER BY day', parameters)

    @staticmethod
    def executions_descending(connection, parameters=()):
        return connection.execute('SELECT execution_payload FROM daily_results WHERE execution_payload IS NOT NULL ORDER BY day DESC', parameters)

    @staticmethod
    def latest_valuation(connection, parameters=()):
        return connection.execute("SELECT valuation_payload FROM daily_results\n                WHERE day<=? AND valuation_status='success' AND valuation_payload IS NOT NULL\n                ORDER BY day DESC LIMIT 1", parameters)

    @staticmethod
    def all_valuations(connection, parameters=()):
        return connection.execute("SELECT valuation_payload FROM daily_results WHERE valuation_status='success' AND valuation_payload IS NOT NULL ORDER BY day", parameters)

    @staticmethod
    def result_for_day(connection, parameters=()):
        return connection.execute('SELECT * FROM daily_results WHERE day=?', parameters)

    @staticmethod
    def table_exists(connection, parameters=()):
        return connection.execute("SELECT 1 FROM sqlite_master WHERE name='daily_results'", parameters)

    @staticmethod
    def first_execution(connection, parameters=()):
        return connection.execute('SELECT execution_payload FROM daily_results WHERE execution_payload IS NOT NULL ORDER BY day LIMIT 1', parameters)
