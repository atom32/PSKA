# PSKA 多租户 Workspace 与 Writing E2E

## Workspace 布局

PSKA 的本地 workspace 根目录默认是：

```text
~/PSKA_workspaces
```

系统进程目录和用户内容目录分开：

```text
~/PSKA_workspaces/_system/run
~/PSKA_workspaces/_system/logs
~/PSKA_workspaces/_system/imports
~/PSKA_workspaces/_system/twitter_archive
~/PSKA_workspaces/tenants/<tenant_id>/users/<user_id>/notes
```

`./start.sh` 只负责启动 PSKA stack 和准备 `_system` 目录；它不会再自动创建或同步 `default/notes`。用户资料必须显式用 tenant/user 导入：

```bash
./scripts/pska --config ".pska/config.json" knowledge-source add-folder \
  --tenant-id "tenant_default" \
  --owner-user-id "user_primary" \
  --space-id "private_primary" \
  --path "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/notes"

./scripts/pska --config ".pska/config.json" files-sync \
  --tenant-id "tenant_default" \
  --owner-user-id "user_primary" \
  --root "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/notes"
```

如果同步路径位于 `~/PSKA_workspaces/tenants/...` 下，CLI 会校验该路径属于传入的 `tenant_id/owner_user_id`。外部显式授权目录仍可作为 knowledge source，但数据库对象会按传入 tenant/user 写入。

## Logout

Gateway 已提供 `/logout`。前端在已登录状态显示当前 user/tenant 和“退出登录”按钮；退出时会清除浏览器 `sessionStorage` 中的本地 PSKA identity，再跳转 `/logout` 清除 HttpOnly gateway session cookie。

浏览器不会持有 AuthNode admin token、PSKA service token、FastReAct service token 或下游 JWT。

## Writing E2E

PSKA 验证必须先通过项目根目录启动：

```bash
./start.sh
```

AuthNode 和 FastReAct 需要由各自项目独立启动；本 E2E 不会启动它们，只检查服务可用性：

```text
AuthNode:  http://127.0.0.1:8788
PSKA API:  http://127.0.0.1:8765
PSKA UI:   http://127.0.0.1:5173
FastReAct: http://127.0.0.1:8000
```

运行：

```bash
./scripts/pska-writing-workspace-e2e --config ".pska/config.json"
```

脚本会：

- 创建隔离 tenant/user。
- 写入一组虚构资料到该用户的 workspace notes 目录。
- 执行 `knowledge-source add-folder`、`files-sync`、`digest-now`。
- 用 Playwright 通过 AuthNode code callback 登录 PSKA Gateway。
- 在 Corpus 页面验证当前用户能看到刚导入的资料。
- 在 Writing Workspace 创建项目，进行并行 Ask、连接答案后的追问、纳入章节和生成 draft。
- 输出 JSON 报告，包含 workspace path、source/digest 摘要、board id、answer count、draft length、citation count。

默认保留 artifacts，方便在 UI 里复查测试项目和语料。

测试语料只用于 E2E，不允许在产品逻辑中加入任何特定公司、特定问题或特定 wording shortcut。PSKA 的回答质量改进必须是领域无关机制。
