# 2026-05-26 Web Search SSL 信任链修复

## 背景

`web_search` / `web_fetch` 在当前 macOS 开发环境下访问 HTTPS 站点时，出现：

- `CERTIFICATE_VERIFY_FAILED`
- `self-signed certificate in certificate chain`

导致 Chat / Study 虽然已经能暴露 Web Search 工具，但外部请求在证书校验阶段失败，最终只能回退到降级回答。

## 本次修复

### 1. 抽出统一 TLS 上下文构建

新增：

- `learning_agent/learning_agent/providers/adapters/tls_utils.py`

提供统一的 HTTPS 信任链构建逻辑，优先级如下：

1. 显式 `web_search.tls_ca_bundle_path`
2. 环境变量：
   - `LA_WEB_SEARCH_CA_BUNDLE_PATH`
   - `SSL_CERT_FILE`
   - `REQUESTS_CA_BUNDLE`
   - `CURL_CA_BUNDLE`
3. 若已安装 `truststore`，优先复用操作系统信任库
4. 最后回退 Python 默认 `ssl.create_default_context()`

### 2. 内置 provider 接入 TLS 配置

更新：

- `builtin_web_search.py`
- `builtin_web_fetch.py`

两者都会在初始化时构建统一 TLS context，并在 `urllib.request.urlopen()` 中显式传入 `context=...`。

### 3. 配置项扩展

新增 Web Search 配置：

- `tls_ca_bundle_path`
- `prefer_system_trust_store`

并接入：

- `config.py`
- `config.example.yaml`
- `config.example.json`

### 4. 依赖补充

在 `requirements.txt` 中加入：

- `truststore>=0.10.0`

用于在可用时直接复用系统信任库，减少 macOS / 企业代理 / 自签根证书场景下的证书问题。

### 5. 错误提示增强

当 TLS 校验失败时，工具错误会附带可执行提示：

- 配置 `web_search.tls_ca_bundle_path`
- 或安装 `truststore`

避免只暴露底层 `SSL` 异常而不给排障方向。

## 测试

新增 / 更新测试覆盖：

- 显式 CA bundle 会被用于构建 TLS context
- `truststore` 可用时优先使用系统信任库
- Web Search service builder 会把 TLS 配置透传给内置 provider

## 结果

本次修复后，Web Search 的 HTTPS 校验链路具备：

- 安全默认
- 系统信任库支持
- 自定义 CA bundle 支持
- 更可解释的错误提示

后续若本机仍因代理根证书失败，只需安装 `truststore` 或配置受信任 PEM 路径，无需再改业务代码。
