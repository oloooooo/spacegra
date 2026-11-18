from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.orm import Thread as ThreadORM
from custom.db.models import Space, UserSpace, UserTables


class UserRepository:
    """UserTables 的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, identity: str) -> UserTables | None:
        return await self.session.get(UserTables, identity)

    async def exists(self, identity: str) -> bool:
        stmt = select(UserTables.identity).where(UserTables.identity == identity)
        return (await self.session.execute(stmt)).scalar() is not None

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count()).select_from(UserTables))).scalar_one())

    async def list_all(self) -> list[UserTables]:
        stmt = select(UserTables).order_by(UserTables.identity)
        return list((await self.session.execute(stmt)).scalars().all())

    async def create(
        self,
        identity: str,
        display_name: str,
        hashed_password: str,
        org_id: str | None = None,
        email: str | None = None,
        is_super: bool = False,
    ) -> UserTables:
        user = UserTables(
            identity=identity,
            display_name=display_name,
            hashed_password=hashed_password,
            org_id=org_id,
            email=email,
            is_super=is_super,
        )
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_password(self, identity: str, hashed_password: str) -> bool:
        """更新密码哈希，返回是否命中行。"""
        stmt = update(UserTables).where(UserTables.identity == identity).values(hashed_password=hashed_password)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]

    async def delete(self, identity: str) -> bool:
        """删除账号，返回是否命中行。"""
        stmt = delete(UserTables).where(UserTables.identity == identity)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]


class SpaceRepository:
    """Space 的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, name: str) -> Space | None:
        return await self.session.get(Space, name)

    async def exists(self, name: str) -> bool:
        stmt = select(Space.name).where(Space.name == name)
        return (await self.session.execute(stmt)).scalar() is not None

    async def list_all(self) -> list[Space]:
        stmt = select(Space).order_by(Space.name)
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, name: str, path: str) -> Space:
        """幂等创建：同名已存在时直接返回既有行（不抛 IntegrityError）。"""
        await self.session.execute(
            pg_insert(Space).values(name=name, path=path).on_conflict_do_nothing(index_elements=[Space.name])
        )
        await self.session.commit()
        space = await self.get(name)
        assert space is not None
        return space

    async def update_path(self, name: str, path: str) -> bool:
        """更新工作目录路径，返回是否命中行。"""
        stmt = update(Space).where(Space.name == name).values(path=path)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]

    async def delete(self, name: str) -> bool:
        """删除空间，返回是否命中行。"""
        stmt = delete(Space).where(Space.name == name)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]


class UserSpaceRepository:
    """UserSpace（用户-微服务空间关联）的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, identity: str, space_name: str) -> UserSpace | None:
        return await self.session.get(UserSpace, (identity, space_name))

    async def exists(self, identity: str, space_name: str) -> bool:
        stmt = select(UserSpace.identity).where(
            UserSpace.identity == identity,
            UserSpace.space_name == space_name,
        )
        return (await self.session.execute(stmt)).scalar() is not None

    async def list_by_user(self, identity: str) -> list[str]:
        """该用户可访问的微服务空间名。"""
        stmt = select(UserSpace.space_name).where(UserSpace.identity == identity).order_by(UserSpace.space_name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_space(self, space_name: str) -> list[UserSpace]:
        stmt = select(UserSpace).where(UserSpace.space_name == space_name).order_by(UserSpace.identity)
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, identity: str, space_name: str) -> UserSpace:
        """幂等分配：已关联时返回既有行，重复调用不报错。"""
        await self.session.execute(
            pg_insert(UserSpace).values(identity=identity, space_name=space_name).on_conflict_do_nothing()
        )
        await self.session.commit()
        link = await self.get(identity, space_name)
        assert link is not None
        return link

    async def delete(self, identity: str, space_name: str) -> bool:
        """解除关联，返回是否命中行。"""
        stmt = delete(UserSpace).where(
            UserSpace.identity == identity,
            UserSpace.space_name == space_name,
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]


class ThreadQuotaRepository:
    """主管线程的配额与级联清理（ADR-0012）。

    子线程的属主是服务账号，所以"一个用户占了多少会话"不能按 `thread.user_id` 算：
    子线程建的时候带了 metadata ``owner_identity``（见 graphs/middleware/async_task_guard.py），
    这里按它计数。只统计主管线程（`parent_thread_id` 为空），子线程跟着父线程走。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def count_owned(self, identity: str) -> int:
        """该用户当前拥有的主管线程数。"""
        stmt = (
            select(func.count())
            .select_from(ThreadORM)
            .where(
                ThreadORM.metadata_json["owner_identity"].astext == identity,
                ThreadORM.metadata_json["parent_thread_id"].astext.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_children(self, parent_thread_id: str) -> list[str]:
        """某个主管线程名下的子线程 id（**只查不删**，顺序由调用方控）。"""
        stmt = select(ThreadORM.thread_id).where(ThreadORM.metadata_json["parent_thread_id"].astext == parent_thread_id)
        return list((await self.session.execute(stmt)).scalars())

    async def delete_threads(self, thread_ids: list[str]) -> None:
        """按 id 删线程行。**调用方必须先删这些线程的 checkpoint**——反序会留孤儿
        （与 aegra 原生 delete_thread 同一约定，见 api/threads.py）。"""
        if not thread_ids:
            return
        await self.session.execute(delete(ThreadORM).where(ThreadORM.thread_id.in_(thread_ids)))
        await self.session.commit()
