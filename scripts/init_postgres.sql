-- 新开发实例的三套隔离数据库；已有 RDS 由管理员分别创建。
CREATE DATABASE taobao OWNER collector;
CREATE DATABASE taobao_poc OWNER collector;
-- 自动化测试只创建临时 schema，不访问上述开发数据库。
CREATE DATABASE collector_test OWNER collector;
