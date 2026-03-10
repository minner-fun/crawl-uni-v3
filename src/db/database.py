import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import Base

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# 优先读取 DATABASE_URL，其次按各字段拼接（便于 Docker / CI 等环境覆盖）
_DATABASE_URL = os.environ.get("DATABASE_URL") or (
    "postgresql+psycopg2://{user}:{passwd}@{host}:{port}/{database}".format(
        user=os.environ.get("DB_USER", "root"),
        passwd=os.environ.get("DB_PASSWD", "12345678"),
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=os.environ.get("DB_PORT", "5432"),
        database=os.environ.get("DB_NAME", "v3info"),
    )
)

engine = create_engine(
    _DATABASE_URL,
    pool_pre_ping=True,   # 每次取连接前 ping 一下，自动重连
    pool_size=5,
    max_overflow=10,
    echo=False,           # 调试时可改为 True 打印 SQL
    # Windows 中文系统 PostgreSQL 默认用 GBK 发送服务端消息，
    # 强制指定 client_encoding=utf8，让服务端以 UTF-8 编码回复，
    # 避免 psycopg2 解码中文错误信息时抛 UnicodeDecodeError。
    connect_args={"options": "-c client_encoding=utf8"},
)

_SessionFactory = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """建表（如果不存在）。程序启动时调用一次即可。"""
    Base.metadata.create_all(engine)


def check_connection() -> bool:
    """测试数据库是否可达，返回 True / False。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    提供一个数据库 Session 的上下文管理器。
    正常退出自动 commit，异常自动 rollback，最终关闭连接。

    用法：
        with get_session() as session:
            repo.upsert_token(session, {...})
    """
    session: Session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    init_db()