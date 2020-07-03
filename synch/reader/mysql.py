import logging
import time
from signal import Signals
from typing import Callable, Dict, Generator, Tuple, Union

import MySQLdb
from MySQLdb.cursors import DictCursor
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.event import QueryEvent
from pymysqlreplication.row_event import DeleteRowsEvent, UpdateRowsEvent, WriteRowsEvent

from synch.broker import Broker
from synch.convert import SqlConvert
from synch.reader import Reader
from synch.redis import RedisLogPos

logger = logging.getLogger("synch.reader.mysql")


class Mysql(Reader):
    only_events = (DeleteRowsEvent, WriteRowsEvent, UpdateRowsEvent, QueryEvent)
    fix_column_type = True

    def __init__(self, source_db: Dict, redis_settings: Dict):
        super().__init__(source_db)
        self.conn = MySQLdb.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            connect_timeout=5,
            cursorclass=DictCursor,
            charset="utf8",
        )
        self.init_binlog_file = source_db.get("init_binlog_file")
        self.init_binlog_pos = source_db.get("init_binlog_pos")
        self.server_id = source_db.get("server_id")
        self.skip_dmls = source_db.get("skip_dmls") or []
        self.skip_delete_tables = source_db.get("skip_delete_tables") or []
        self.skip_update_tables = source_db.get("skip_update_tables") or []
        self.cursor = self.conn.cursor()
        self.databases = list(map(lambda x: x.get("database"), source_db.get("databases")))
        self.pos_handler = RedisLogPos(redis_settings, self.server_id)

    def get_binlog_pos(self) -> Tuple[str, str]:
        """
        get binlog pos from master
        """
        sql = "show master status"
        result = self.execute(sql)[0]
        return result.get("File"), result.get("Position")

    def get_primary_key(self, db, table) -> Union[None, str, Tuple[str, ...]]:
        """
        get pk
        :param db:
        :param table:
        :return:
        """
        pri_sql = f"select COLUMN_NAME from information_schema.COLUMNS where TABLE_SCHEMA='{db}' and TABLE_NAME='{table}' and COLUMN_KEY='PRI'"
        result = self.execute(pri_sql)
        if not result:
            return None
        if len(result) > 1:
            return tuple(map(lambda x: x.get("COLUMN_NAME"), result))
        return result[0]["COLUMN_NAME"]

    def signal_handler(self, signum: Signals, handler: Callable):
        sig = Signals(signum)
        log_f, log_p = self.pos_handler.get_log_pos()
        logger.info(f"shutdown producer on {sig.name}, current position: {log_f}:{log_p}")
        exit()

    def start_sync(self, broker: Broker, insert_interval: int):
        log_file, log_pos = self.pos_handler.get_log_pos()
        if not (log_file and log_pos):
            log_file = self.init_binlog_file
            log_pos = self.init_binlog_pos
            if not (log_file and log_pos):
                log_file, log_pos = self.get_binlog_pos()
            self.pos_handler.set_log_pos_slave(log_file, log_pos)

        log_pos = int(log_pos)
        logger.info(f"mysql binlog: {log_file}:{log_pos}")

        count = last_time = 0
        tables = []
        schema_tables = {}
        for database in self.source_db.get("databases"):
            database_name = database.get("database")
            for table in database.get("tables"):
                table_name = table.get("table")
                schema_tables.setdefault(database_name, []).append(table_name)
                pk = self.get_primary_key(database_name, table_name)
                if not pk or isinstance(pk, tuple):
                    # skip delete and update when no pk and composite pk
                    self.skip_delete_tables.add(f"{database_name}.{table_name}")
                tables.append(table_name)
        only_schemas = self.databases
        only_tables = list(set(tables))
        for schema, table, event, file, pos in self._binlog_reading(
            only_tables=only_tables,
            only_schemas=only_schemas,
            log_file=log_file,
            log_pos=log_pos,
            server_id=self.server_id,
            skip_dmls=self.skip_dmls,
            skip_delete_tables=self.skip_delete_tables,
            skip_update_tables=self.skip_update_tables,
        ):
            if not schema_tables.get(schema) or (table and table not in schema_tables.get(schema)):
                continue
            broker.send(msg=event, schema=schema)
            self.pos_handler.set_log_pos_slave(file, pos)
            logger.debug(f"send to queue success: key:{schema},event:{event}")
            logger.debug(f"success set binlog pos:{file}:{pos}")

            now = int(time.time())
            count += 1

            if last_time == 0:
                last_time = now
            if now - last_time >= insert_interval:
                logger.info(f"success send {count} events in {insert_interval} seconds")
                last_time = count = 0

    def _binlog_reading(
        self,
        only_tables,
        only_schemas,
        log_file,
        log_pos,
        server_id,
        skip_dmls,
        skip_delete_tables,
        skip_update_tables,
    ) -> Generator:
        stream = BinLogStreamReader(
            connection_settings=dict(
                host=self.host, port=self.port, user=self.user, passwd=self.password,
            ),
            resume_stream=True,
            blocking=True,
            server_id=server_id,
            only_tables=only_tables,
            only_schemas=only_schemas,
            only_events=self.only_events,
            log_file=log_file,
            log_pos=log_pos,
            fail_on_table_metadata_unavailable=True,
            slave_heartbeat=10,
        )
        for binlog_event in stream:
            if isinstance(binlog_event, QueryEvent):
                schema = binlog_event.schema.decode()
                query = binlog_event.query.lower()
                if "alter" not in query:
                    continue
                try:
                    convent_sql = SqlConvert.to_clickhouse(schema, query)
                except Exception as e:
                    convent_sql = ""
                    logger.error(f"query convert to clickhouse error, error: {e}, query: {query}")
                if not convent_sql:
                    continue
                event = {
                    "table": None,
                    "schema": schema,
                    "action": "query",
                    "values": {"query": convent_sql},
                    "event_unixtime": int(time.time() * 10 ** 6),
                    "action_seq": 0,
                }
                yield schema, None, event, stream.log_file, stream.log_pos
            else:
                schema = binlog_event.schema
                table = binlog_event.table
                skip_dml_table_name = f"{schema}.{table}"
                for row in binlog_event.rows:
                    if isinstance(binlog_event, WriteRowsEvent):
                        event = {
                            "table": table,
                            "schema": schema,
                            "action": "insert",
                            "values": row["values"],
                            "event_unixtime": int(time.time() * 10 ** 6),
                            "action_seq": 2,
                        }

                    elif isinstance(binlog_event, UpdateRowsEvent):
                        if "update" in skip_dmls or skip_dml_table_name in skip_update_tables:
                            continue
                        delete_event = {
                            "table": table,
                            "schema": schema,
                            "action": "delete",
                            "values": row["before_values"],
                            "event_unixtime": int(time.time() * 10 ** 6),
                            "action_seq": 1,
                        }
                        yield binlog_event.schema, binlog_event.table, delete_event, stream.log_file, stream.log_pos
                        event = {
                            "table": table,
                            "schema": schema,
                            "action": "insert",
                            "values": row["after_values"],
                            "event_unixtime": int(time.time() * 10 ** 6),
                            "action_seq": 2,
                        }

                    elif isinstance(binlog_event, DeleteRowsEvent):
                        if "delete" in skip_dmls or skip_dml_table_name in skip_delete_tables:
                            continue
                        event = {
                            "table": table,
                            "schema": schema,
                            "action": "delete",
                            "values": row["values"],
                            "event_unixtime": int(time.time() * 10 ** 6),
                            "action_seq": 1,
                        }
                    else:
                        return
                    yield binlog_event.schema, binlog_event.table, event, stream.log_file, stream.log_pos