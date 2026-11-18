# wechat-mp-export

[English Documentation](./README.en.md)

一个通用、可断点续传、面向本地归档与静态博客/CMS 迁移的**微信公众号全量文章与媒体导出工具**。

支持将任意微信公众号的历史图文、贴图画廊（“小绿书”）、封面、正文配图、内嵌语音（`<mpvoice>`）和内嵌视频完整下载到本地，生成带 YAML Frontmatter 的 **Markdown**、清洗后的标准 **HTML**、可离线检索的本地网页目录、CSV 台账，以及可直接增量导入 **Halo 2.x** 的归档包。

---

## 核心特性

- **多通道历史文章发现（突破目录翻页上限）**
  1. **微信读书目录通道**：通过官方扫码授权拉取公众号最新文章目录，自动节流、断点续传与长效凭证静默续期。
  2. **公开合集（`appmsgalbum`）全量遍历**：自动从文章中发现公众号合集 ID 并翻页抓取全部早期合集文章，合并合集标签。
  3. **文内历史互链多轮深挖（Multi-hop BFS）**：递归提取文章正文中引用的同账号历史推文（自动过滤外部公众号链接），批量将长链接转换为免验证短链，并按 `(mid, idx)` 与 `short_id` 严格去重。
  4. **免登录模式（`--no-weread`）**：无需扫码登录，仅凭单篇示例链接、URL 列表文件或合集链接，即可完成单篇导出或基于公开合集 + 内链的多轮扩展归档。
- **全类型正文与多媒体本地化**
  - 支持标准图文（`#js_content`）、分享卡片跳转（`js_share_source`）及**图片画廊/贴图文章**（`picture_page_info_list` + `window.desc`）。
  - 多线程无 Cookie / 无 Referer 隔离下载微信 CDN 图片，基于**文件头魔数（Magic Bytes）**自动修正真实扩展名（如 `jpg` / `png` / `webp` / `gif` / `svg` / `avif`）。
  - 自动下载 `<mpvoice>` 语音与 `mpvideo` 视频流及封面，转换为标准 HTML5 `<audio>` / `<video>` 标签。
  - 自动清除微信 `data-src`、`visibility: hidden`、`1px` 占位等懒加载陷阱，开箱即用。
- **多格式开放归档输出**
  - 单篇输出：`article.md`（GFM Markdown + YAML Frontmatter，适配 Hugo / Hexo / Astro / VitePress / Obsidian）、`content.html`、`preview.html`、可选 `article.pdf`（`--pdf`）、`raw.html` 与 `meta.json`（含 SHA-256 校验值）。
  - 全局索引：支持即时搜索的离线总目录 `index.html`、Excel 友好的 `articles_index.csv`、`manifest.json`、`EXPORT_REPORT.md` 与 `halo_import_bundle.json`。
- **内置 Halo 2.x 幂等导入器（可选）**
  - `wechat-mp-export import-halo` 支持通过 **Personal Access Token（`HALO_PAT`）** 或控制台登录将归档文章与媒体附件增量上传、重写站内永久链接、创建分类/标签并发布，重复执行自动跳过已发布文章。
- **隐私与凭据安全设计（Security by Design）**
  - 登录会话默认保存在仓库外的用户配置目录（Linux 下为 `~/.config/wechat-mp-export/`，权限 `0700`/`0600`，自动写入 `.gitignore`）。自定义状态目录时，也应选择仓库和归档目录以外的位置。
  - 导出清单的本地文件引用使用归档相对路径，不复制登录会话。归档会保留公众号名称、文章链接、作者及正文等来源数据，分享前需单独审查。
  - 内置 `wechat-mp-export audit` 隐私与密钥扫描命令，开源发布或分享归档前可一键自检。

---

## 安装

要求 Python **3.10+**。

```bash
git clone https://github.com/qiao-y/wechat-mp-export.git
cd wechat-mp-export

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

可选扩展依赖：

```bash
# 可选：启用 Playwright 无头浏览器风控兜底
pip install -e ".[browser]"
playwright install chromium

# 可选：Halo 账号密码表单登录的纯 Python RSA 加密支持（使用 HALO_PAT 令牌方式则无需安装）
pip install -e ".[halo]"
```

---

## 快速上手

### 方式一：完整公众号导出（推荐：扫码授权 + 合集与内链深挖）

#### 1. 微信扫码授权（仅保存在本机 `~/.config/wechat-mp-export`）

```bash
wechat-mp-export auth login
```

终端会直接渲染二维码（同时保存临时二维码图片并打印官方扫码链接）。使用手机微信扫码确认后，临时二维码自动清理，本地仅以 `0600` 权限保存会话。

> 若在无交互脚本或远程环境中运行，也可分步执行：
> ```bash
> wechat-mp-export auth start --qr-output ./tmp_qr.png
> wechat-mp-export auth poll
> ```

#### 2. 导出目标公众号全部文章

最精准的方式是传入该公众号**任意一篇文章的分享链接**（工具会自动解析公众号 `__biz` / `bookId`、名称与简介，拉取目录并递归扫描全部公开合集与文内历史互链）：

```bash
wechat-mp-export export \
  --article-url "https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_ID" \
  --out-dir "./exports/demo-account"
```

也可以按公众号名称检索或直接指定 `bookId` / `__biz`：

```bash
# 先检索公众号标识
wechat-mp-export search "示例公众号"

# 按名称或 bookId 导出（例如仅导出最新 20 篇可加 -n 20 或 --max-articles 20）
wechat-mp-export export --account "示例公众号" --max-articles 20 --out-dir "./exports/demo-account"
wechat-mp-export export --book-id "MP_WXS_1234567890" --out-dir "./exports/demo-account"
```

---

### 方式二：免登录导出（单篇文章 / URL 列表 / 公开合集）

无需运行 `auth login`，直接加 `--no-weread` 即可：

```bash
# 1. 从单篇种子文章出发，自动递归抓取该公众号的公开合集与文内互链
wechat-mp-export export \
  --no-weread \
  --article-url "https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_ID" \
  --out-dir "./exports/public-crawl"

# 2. 仅导出指定的若干篇文章（关闭合集与内链扩展）
wechat-mp-export export \
  --no-weread \
  --no-deep-discover \
  --url "https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_1" \
  --url "https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_2" \
  --out-dir "./exports/selected-posts"

# 3. 批量导出 URL 列表文件或指定公开合集链接
wechat-mp-export export --no-weread --url-file ./urls.txt --out-dir "./exports/from-list"
wechat-mp-export export --no-weread --album-url "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=MTIzNDU2Nzg5MA==&album_id=1800000000000001"
```

---

## 归档目录结构

```text
exports/demo-account/
├── index.html                  # 本地离线总目录（支持关键词与标签即时筛选）
├── manifest.json               # 机器可读归档清单（本地文件引用为相对路径，保留来源元数据）
├── articles_index.csv          # Excel 可直接打开的 UTF-8-BOM 台账表
├── EXPORT_REPORT.md            # 导出统计报告
├── halo_import_bundle.json     # Halo 2.x 便携导入包
├── account_info.json           # 公众号公开元数据
└── articles/
    └── 2026-05-01_AbCdEf123456_文章标题/
        ├── article.md          # GFM Markdown + YAML Frontmatter
        ├── content.html        # 清洗后的标准 HTML 正文片段（媒体已全部替换为本地路径）
        ├── preview.html        # 单篇独立离线阅读网页
        ├── raw.html            # 原始网页备份（可用 --no-raw-html 关闭）
        ├── meta.json           # 单篇元数据与媒体文件 SHA-256 清单
        └── media/              # 本地化封面、配图、语音 (.mp3) 与视频 (.mp4)
```

---

## 导入到 Halo 2.x（可选）

导出与导入完全解耦。确认本地归档无误后，可在 Halo 个人中心生成 **Personal Access Token（PAT）** 并通过环境变量传入：

```bash
# 先执行 dry-run 预览将要导入的文章与附件数量
wechat-mp-export import-halo \
  --bundle-dir "./exports/demo-account" \
  --url "https://blog.example.com" \
  --dry-run

# 正式执行幂等增量导入（推荐通过环境变量传入令牌，避免写入 shell history）
export HALO_PAT="pat_xxxxxxxxxxxxxxxxxxxxxxxx"
wechat-mp-export import-halo \
  --bundle-dir "./exports/demo-account" \
  --url "https://blog.example.com" \
  --owner "target-author-username" \
  --category "公众号归档"
```

导入进度保存在归档目录下的 `halo_import_state.json`。后续增量抓取新文章后重新运行同一命令，已发布的文章与已上传附件会自动跳过。

---

## 常用命令行子命令与参数

| 命令 | 说明 |
| --- | --- |
| `wechat-mp-export auth login` | 终端交互式扫码登录（凭据以 `0600` 权限保存在 `~/.config/wechat-mp-export`） |
| `wechat-mp-export auth start` / `auth poll` | 非阻塞生成二维码 / 轮询确认结果 |
| `wechat-mp-export auth status` / `auth logout` | 查看脱敏登录状态 / 彻底删除本机登录凭据 |
| `wechat-mp-export search <关键词>` | 在已关注列表与全局索引中检索公众号 `bookId` 与 `__biz` |
| `wechat-mp-export export ...` | 执行全量或增量导出（支持断点续传） |
| `wechat-mp-export rebuild-index <目录>` | 根据已有 `articles/*/meta.json` 重新生成索引、CSV 与 Halo 导入包 |
| `wechat-mp-export import-halo ...` | 将归档包幂等导入 Halo 2.x |
| `wechat-mp-export audit [目录]` | 扫描代码目录或导出目录是否包含敏感凭据文件、密钥或本机绝对路径 |

`export` 常用可选参数：
- `--workers 8`：并发下载微信 CDN 图片的线程数（默认 `8`）
- `--delay 0.65`：相邻文章抓取平均间隔秒数（对数正态抖动节流）
- `-n N` / `--max-articles N`（兼容别名 `--limit N`）：限制最多导出的文章总篇数（`0` 为不限；达到 `N` 篇即立即停止）
- `--max-new N`：限制本次运行新下载的文章篇数（适合增量分批抓取）
- `--order newest|oldest`：指定优先导出的时间顺序（默认 `newest`，即优先导出最新文章）
- `--pdf`：调用本机无头 Chrome / Edge / Chromium 额外渲染 `article.pdf`
- `--no-raw-html`：不保留原始 `raw.html` 以节省磁盘空间
- `--no-filter` / `--filter-rules rules.json`：关闭或自定义底部二维码/横幅广告清洗规则

---

## 隐私与安全自检

在将代码提交到公开仓库或对外分享导出归档前，可运行内置审计工具与单元测试：

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m wechat_mp_export audit .
```

`audit` 是基于文件名与文本模式的预检查，不能证明不存在所有个人信息。它跳过 Git 历史、部分缓存目录、常见二进制格式、无法解码及超过 8 MiB 的文件。发布前还应检查隐藏文件、编辑器备份、构建包及 Git 历史；不要将登录状态目录或实际导出的归档一起上传。`.gitignore` 不会从已跟踪文件或压缩包中移除敏感内容。

源码中的 `DEFAULT_GUEST_TOKEN` 是上游微信读书协议实现的共享访客签名默认值，可在[固定版本源码](https://github.com/x554960766/wechat-mp-tools/blob/68fab018e5d17b1c506e2cc105efc970b3cd99ac/backend/weread_signer.py#L20-L21)核对。它不是扫码后取得的个人登录令牌；访问个人账号仍需要运行时取得的 `accessToken`、`refreshToken` 等私有会话信息。

---

## 说明与致谢

- **公开历史范围说明**：当上游目录接口对超早历史翻页受限时，本工具会自动通过**公开文章合集遍历 + 文内互链多轮深挖**找回早期文章；若极早期某篇孤立文章既不在近期目录内、未加入任何合集，也从未被其他文章引用过，则公开渠道无法枚举到该链接（但只要拿到链接即可通过 `--url` 补充归档）。
- **免责声明**：本工具仅供个人学习研究、自有公众号内容备份及经授权的资料归档使用。请遵守目标平台服务条款与原作者版权，合理控制抓取频率。
- **开源致谢**：部分协议兼容性设计与解析思路参考了开源社区项目 [wechat-mp-tools](https://github.com/x554960766/wechat-mp-tools)（MIT）、[we-mp-rss](https://github.com/rachelos/we-mp-rss) 与 [wechatDownload](https://github.com/qiye45/wechatDownload)，特此致谢。

## License

[MIT License](./LICENSE)
