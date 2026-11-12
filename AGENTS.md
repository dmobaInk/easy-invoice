# easy-invoice 项目约定

## 生成更新包

用户在本项目中说“生成更新包”“生成更新补丁”或表达同样意图时，直接生成供 `scripts/invoice-update` 使用的 Git 补丁。

1. 默认以请求开始时当前分支的最后一次本地提交（`git rev-parse HEAD`）为基线，对比当前工作区的最终内容。用户明确指定基线时使用指定版本。若基线与工作区之间没有可打包的应用改动，说明情况即可，不回退到 `HEAD~1`，也不交付旧补丁。
2. 检查暂存、未暂存和未跟踪的新文件，包括新增、修改和删除。可在临时目录使用独立的 `GIT_INDEX_FILE`，用 `git read-tree <基线>` 初始化，再用 `git add -A -- app/` 收集工作区内容，复核文件清单后用 `git diff --cached --no-renames --binary <基线> -- app/` 生成补丁。保留用户原有暂存区和工作区。
3. 以 `scripts/invoice-update` 中的 `patch_paths` 为准核对可安装路径。检查应用目录外的差异是否包含此次功能依赖的 SQL、依赖库或配置变更。若有必需改动超出更新器支持范围，应说明如何配套更新，不能把缺少运行依赖的补丁当作完整更新交付。
4. 在基线提交的独立临时副本上调用更新器的路径校验，执行 `git apply --check`、实际应用、相关测试及 `git apply --reverse --check`。验证成功后将补丁保存到 `dist/`，用功能名或版本标识命名。
5. 回复给出补丁链接、基线短提交号、验证结果及上线用法：将文件上传为 `/opt/easy-invoice-updates/update.patch`，执行 `sudo invoice-update update`。生成补丁本身无需提交、推送或部署。
