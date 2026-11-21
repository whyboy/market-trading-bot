"""账户表的数据访问。"""


class AccountRepository:
    @staticmethod
    def insert_account(connection, parameters=()):
        return connection.execute('INSERT INTO account(id, initial_cash, instance_id, strategy_id, created_at)\n            VALUES (1, ?, ?, ?, ?)', parameters)

    @staticmethod
    def create_table(connection, parameters=()):
        return connection.execute("CREATE TABLE IF NOT EXISTS account (\n                id INTEGER PRIMARY KEY CHECK (id=1), initial_cash TEXT NOT NULL,\n                instance_id TEXT NOT NULL DEFAULT '', strategy_id TEXT NOT NULL DEFAULT '',\n                created_at TEXT NOT NULL DEFAULT '')", parameters)

    @staticmethod
    def assign_instance(connection, parameters=()):
        return connection.execute("UPDATE account SET instance_id=? WHERE instance_id=''", parameters)

    @staticmethod
    def assign_strategy(connection, parameters=()):
        return connection.execute("UPDATE account SET strategy_id=? WHERE strategy_id=''", parameters)

    @staticmethod
    def get_initial_cash(connection, parameters=()):
        return connection.execute('SELECT initial_cash FROM account WHERE id=1', parameters)

    @staticmethod
    def get_strategy(connection, parameters=()):
        return connection.execute('SELECT strategy_id FROM account WHERE id=1', parameters)

