import os

import psycopg2
import pytest

from synch import get_reader
from synch.factory import init


@pytest.fixture(scope="session", autouse=True)
def initialize_tests():
    local = os.getenv('local') == 'True'
    if local:
        init('synch.yaml')
    else:
        init('tests/synch.yaml')


@pytest.fixture(scope="session", autouse=True)
def create_mysql_table(initialize_tests):
    sql = """use test;CREATE TABLE IF NOT EXISTS `test.test` (
  `id` int NOT NULL AUTO_INCREMENT,
  `amount` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=10 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""
    return get_reader("mysql_db").execute(sql)


@pytest.fixture(scope="session", autouse=True)
def create_postgres_table(initialize_tests):
    sql = """create table if not exists test
(
    id     int NOT NULL primary key,
    amount decimal(10, 2) DEFAULT NULL
)"""
    try:
        get_reader("postgres_db").execute(sql)
    except psycopg2.ProgrammingError as e:
        assert str(e) == "no results to fetch"