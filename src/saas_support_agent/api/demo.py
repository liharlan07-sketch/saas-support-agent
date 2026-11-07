"""显式创建全新合成数据库；已有文件一律拒绝覆盖。"""
from argparse import ArgumentParser
from pathlib import Path

from saas_support_agent.merchant_demo import seed_demo
from saas_support_agent.repositories.database import Database


def main():
    parser = ArgumentParser()
    parser.add_argument('--database', type=Path, default=Path('runtime/http-demo.sqlite3'))
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with args.database.open('xb'):
        pass
    database = Database(args.database)
    database.initialize()
    seed_demo(database)
    print('合成演示数据库已创建。未启动服务，也未配置身份令牌。')


if __name__ == '__main__':
    main()
