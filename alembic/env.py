from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
import sys
from os.path import abspath, dirname

# ---------- 关键修改 1：添加项目根目录到系统路径 ----------
# 确保 Alembic 能找到您的模型文件
sys.path.insert(0, dirname(dirname(abspath(__file__))))  # 假设模型文件在项目根目录

# ---------- 关键修改 2：导入您的模型基类 ----------
from fx_bot import Base  # 替换为您的实际模型文件路径

# Alembic 配置对象
config = context.config

# 配置日志（保留原有代码）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------- 关键修改 3：设置元数据 ----------
target_metadata = Base.metadata  # 原为 None

def run_migrations_offline() -> None:
    """离线模式迁移（用于生成SQL脚本）"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,  # 确保这里使用正确的元数据
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """在线模式迁移（直接操作数据库）"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata  # 确保这里使用正确的元数据
        )
        with context.begin_transaction():
            context.run_migrations()

# 根据模式选择迁移方式
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()