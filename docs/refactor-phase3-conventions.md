# Phase 3 共用约定

> 配套 `docs/refactor-plan-sqlite.md`（主计划）。三个 stage 的 runner 改造时遵循这里的约定，避免接口分歧。

## 1. 设置

`BiliSettings.bili_db_dir` 已加（默认 `"data/bili"`）。所有 SQLite 路径从这里派生。
旧的 `bili_fetching_data_dir` / `bili_fetching_error_dir` / `bili_parsing_data_dir` / `bili_processing_data_dir` / `bili_processing_error_dir` / `bili_manifest_dir` **暂留不删**——它们没人读了，但删除留到 Phase 4，避免破坏正在改的代码读到一半的引用。

## 2. UidContext 生命周期：每次 cmd 调用自开自关

不缓存、不池化。`Command.{fetch,parse,process}_uid(uid, ...)` 实现：

```python
async def fetch_uid(self, uid: int, ...) -> CommandResult:
    ctx = UidContext(uid, self._settings.bili_db_dir)
    await ctx.open()
    try:
        store = FetchingStore(ctx)
        # ... runner work ...
    finally:
        await ctx.close()
```

WAL + sqlite open 大概 1ms 量级；同 uid 串跑 fetch→parse→process 多开两次 conn 是可接受的成本，换来零状态复杂度。

## 3. Command 构造签名

每个 stage 的 `Command` 不再持有 `data` / `error` store——store 是请求级的，每次 `*_uid` 现造。改持有 `settings` 与跨请求服务（`RateLimitController` / `asr_backend` / `credential_provider` / `fetching_query` 等）：

```python
class FetchingCommand:
    def __init__(
        self,
        settings: BiliSettings,
        rl: RateLimitController,
        *,
        stale_running_threshold_ms: int,
    ) -> None: ...
    async def fetch_uid(self, uid, endpoints, mode) -> CommandResult: ...
    async def delete_uid(self, uid) -> dict[str, int]: ...
    async def close(self) -> None: ...
```

`delete_uid` 实现 = 删两个 db 文件 + `rmtree(workdir)`，统计返回 `{"main_db": 0/1, "raw_db": 0/1, "workdir": int_files_removed}`。

## 4. assemble() 改造（每 stage）

每 stage 的 `assemble(settings, ...)` 不再返回 `(cmd, qry, data, error)`——`qry` 在 Phase 4 删，`data` / `error` 不复存在。新签名：

```python
async def assemble(settings: BiliSettings, ...) -> FetchingCommand: ...
async def assemble(settings: BiliSettings, ...) -> ParsingCommand: ...
async def assemble(settings: BiliSettings, ...) -> ProcessingCommand: ...
```

返回单值。`bili_unit/__init__.py::assemble()` Phase 4 才改，先不动；它当前的 unpacking 是 `(fetch_cmd, fetch_qry, _fetch_data, _fetch_error)` 这种四元组——为了让现存的 unit-level `assemble()` 还能跑、Phase 3 不破坏入口，三个 stage 的 `assemble()` 临时**返回三元组 `(cmd, None, None)` 或在 stage 内部提供向后兼容签名**。

> **更简单的妥协**：stage 的 `assemble()` 直接返回新单值 `cmd`；`bili_unit/__init__.py::assemble()` 同步更新 unpacking 也是简单一行 `fetch_cmd = await _fetching_assemble(settings)`。这比维护兼容签名干净。Phase 3 就这么改。

## 5. Runner 接口变化

Runner 的构造签名可以**保持**：还是接受能写入存储的对象。但语义换了：

```python
# 旧
class Runner:
    def __init__(self, data: DataStore, error: ErrorStore, rl, settings): ...

# 新
class Runner:
    def __init__(self, store: FetchingStore, rl: RateLimitController, settings: BiliSettings): ...
```

`store` 一个对象同时承担旧 `data` + `error` 的职责。Runner 内部所有 `self._data.X(...)` / `self._error.X(...)` 改为 `self._store.X(...)`。

## 6. 关键语义变化（必须改 runner 内部逻辑，不只是搬调用）

参见每个 store 的 Phase 2 mapping 表，重申最关键的 6 点：

1. **Envelope 折叠**：`raw_payload` 表存的是**内层**响应 dict，不是 envelope。runner 里所有 `existing.get("raw_payload", {})` 改成 `existing` 直接用。
2. **删 `rate_limit` 持久化**：runner 里 4 处 `data.put("rate_limit:...")` 全删；同步删 `RateLimitController.to_state()` 方法。
3. **`failed_item_ids` 不再持久化**：终结状态时 `await store.list_errors(...)` / `list_failed_audio_bvids()` 现算。
4. **progress shape 简化**：`{cursor, total, fetched}`。`done` 由 `cursor IS NULL` 推断；`mode` 由 spec 推断。
5. **task 拆解**：`init_task([eps])` 一次 → 之后 `update_endpoint_state(...)` / `update_task_status(...)` 增量。
6. **delete 路径**：store 不暴露 `delete_by_uid`，命令层用文件 IO 删。

## 7. 测试约定

每个 subagent 负责让自己 stage 现存测试**全绿**。具体：

- 旧 `conftest.py` 的 `stores` / `runner` / `command` / `query` fixture 依旧在用——**保留并更新它们的实现**，让旧测试不动逻辑就能跑。
  - 旧 `stores` fixture 返回 `(ds, es)` → 新版返回 `(ctx, store)` 或类似形态，subagent 自己定，但要把测试 import 改对应。
  - 关键是**测试用例本身能不动则不动**——只动 fixture 与 import。
- 涉及**直接 SQL 断言**的测试：用 `ctx.main.fetch_one(...)` 写新断言（参考 P2 的三套测试）。
- **删除整个文件**清单（Phase 6 真正删，Phase 3 先 `pytest.skip` 整文件）：
  - `test_storage_kv_contract.py` — 旧底层契约测试
  - `test_manifest.py` — manifest 计算被替换为 SQL VIEW，测试整体作废
  - `test_sdk_session.py` 中的 query 部分 — 这个 Phase 4 处理；Phase 3 暂时让它能跑
- 不要碰 `bili_unit/tests/test_db_skeleton.py` / `test_*_store_sqlite.py` 这四个新文件——它们已经独立。

## 8. 你**不要**碰

- `bili_unit/_storage/` —— Phase 3 末尾我串行删
- `bili_unit/_aggregates.py` / `bili_unit/_manifest.py` —— Phase 4 删
- `bili_unit/query/` / 三个 `*/query.py` —— Phase 4 删
- `bili_unit/__init__.py` 的 `BiliQuery` 入口 —— Phase 4 改
- `bili_unit/__main__.py` —— Phase 5 改
- `BiliCommand` (`bili_unit/command/__init__.py`) —— Phase 4 改

但需要注意：当前 `BiliCommand.__init__` 接收 `parsing` / `processing` 命令实例。三个 stage 的 `assemble()` 返回值变了之后，`bili_unit/__init__.py::assemble()` 那个 unit-level 函数会编译错。**允许**改 `bili_unit/__init__.py::assemble()` 的 unpacking 一处（必要时），不允许改 `BiliCommand` 与 `BiliQuery` 类。

## 9. 验证

每个 subagent 改完，运行：

```
.venv/Scripts/python.exe -m pytest bili_unit/tests/ -q
```

要求：**0 failed, 0 errored**。允许出现 `skipped`（你 skip 的整文件）。

如果某个 stage 的现存测试与 store API 实在合不上，可以单独 `pytest.skip(reason="moved to Phase 6 rewrite")` 整个文件——但只能 skip 不能 delete，Phase 6 才删。
