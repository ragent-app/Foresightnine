可以。下面给你一套我建议直接照着部署的 **Cloudflare 全免费 V1 方案**。我按“公开 Web App，而不是 50 人内部工具”设计，所以**普通用户不使用 Cloudflare Access**，避免后续用户数导致收费。

截至 **2026-08-31**，这套方案可以保持 `$0/月`：Workers Free 为 10 万请求/天，D1 为 500 万行读/天、10 万行写/天、5GB 总存储，Workers Builds 免费 3000 分钟/月；静态 Assets 请求免费且不限量。([Cloudflare Docs][1])

---

# 一、最终架构

假设你的域名是：

```text
example.com
```

我建议：

```text
https://example.com
        ↓
Landing Page（可选）

https://app.example.com
        ↓
┌───────────────────────────────────────┐
│            Cloudflare                 │
│                                       │
│ DNS + SSL + CDN                       │
│          │                            │
│          ▼                            │
│ Workers Static Assets                 │
│ React/Vue/Svelte 前端                 │
│          │                            │
│          ├── /assets/* → 免费静态文件 │
│          │                            │
│          └── /api/*                   │
│                ↓                      │
│         Cloudflare Worker             │
│                │                      │
│     ┌──────────┼──────────┐           │
│     ▼          ▼          ▼           │
│ Google OAuth GitHub OAuth  D1         │
│                           │           │
│                     users/session     │
└───────────────────────────────────────┘
```

身份验证：

```text
Google ───┐
          ├── Better Auth
GitHub ───┘       │
                  ▼
             Worker
                  │
                  ▼
                  D1
```

推荐用 **Better Auth**，原因是它本身开源，已经原生支持 Cloudflare D1，Google/GitHub OAuth 都支持，不需要 Clerk/Auth0/Supabase Auth 之类额外 SaaS。Better Auth 1.5 起已经加入 D1 原生支持，当前 1.7.2 还专门修复了 Cloudflare D1 migration。([Better Auth][2])

---

# 二、免费方案的费用边界

首先确认 Cloudflare：

```text
Dashboard
→ Billing
→ Workers
```

保持：

```text
Workers Free
```

不要开：

```text
Workers Paid
Cloudflare Pro
R2 Paid addons
Zero Trust Paid
```

免费额度：

| 项目             |         Free |
| -------------- | -----------: |
| 静态网页/JS/CSS    |      免费、不限请求 |
| Worker         | 100,000 请求/天 |
| Worker CPU     |      10ms/请求 |
| D1 rows read   |  5,000,000/天 |
| D1 rows write  |    100,000/天 |
| D1 storage     |  5GB/account |
| 单个 D1 DB       |        500MB |
| Workers Builds |    3000 分钟/月 |
| GitHub 自动部署    |           免费 |
| SSL            |           免费 |
| DNS            |           免费 |
| Google OAuth   |           免费 |
| GitHub OAuth   |           免费 |
| Turnstile      |           免费 |

([Cloudflare Docs][1])

如果免费额度超过：

> Free 计划不会因为 Worker/D1 超额自动给你按量扣钱；请求/数据库操作会开始失败，需要等待额度重置或者主动升级。D1 官方明确说明免费日限额达到后查询会报错。([Cloudflare Docs][3])

---

# 三、域名先放到 Cloudflare

如果你的域名本来就在 Cloudflare：

```text
Cloudflare
→ Websites
→ example.com
```

看到：

```text
Status: Active
```

这一部分就完成。

我们最终用：

```text
app.example.com
```

**现在不用手动创建 DNS A/CNAME。**

后面把 Worker 绑定 Custom Domain 时，Cloudflare 会自动：

```text
创建 DNS
+
签发 HTTPS 证书
+
维护证书
```

这是官方推荐的 Worker-origin 部署方式。([Cloudflare Docs][4])

---

# 四、代码仓库结构

如果是典型 React/Vite 项目，我建议：

```text
foresightnine/
│
├── src/
│   ├── frontend/
│   │   └── ...
│   │
│   ├── worker/
│   │   ├── index.ts
│   │   └── auth.ts
│   │
│   └── ...
│
├── dist/
├── package.json
├── vite.config.ts
├── wrangler.jsonc
├── .gitignore
└── .dev.vars        # 绝不提交 Git
```

不过不要求一定改成这个目录。

Cloudflare 只关心两件事：

```text
frontend build → dist/

Worker entry → src/worker/index.ts
```

Workers 现在可以同时部署 Worker 代码和 React/Vue/Svelte 产生的静态文件。([Cloudflare Docs][5])

---

# 五、安装 Cloudflare + Auth 依赖

项目目录：

```bash
npm install better-auth hono
npm install -D wrangler
```

如果已经有 Wrangler/Hono，不重复装即可。

检查：

```bash
npx wrangler --version
```

Cloudflare 现在推荐新项目使用：

```text
wrangler.jsonc
```

而不是旧教程里大量出现的：

```text
wrangler.toml
```

([Cloudflare Docs][6])

---

# 六、创建 Cloudflare Worker

如果你的代码已经在 GitHub，我实际上推荐**直接从 GitHub 建 Worker**。

进入：

```text
Cloudflare Dashboard
→ Workers & Pages
→ Create application
→ Import a repository
```

选择：

```text
GitHub
```

Cloudflare 会要求安装：

```text
Cloudflare Workers & Pages GitHub App
```

只授权需要的 repository 即可。

例如：

```text
seethelightluo/foresightnine
```

production branch：

```text
main
```

之后：

```text
git push origin main
```

就会自动：

```text
GitHub
 ↓
Cloudflare Build
 ↓
Cloudflare Worker
 ↓
Production
```

Cloudflare 当前官方 GitHub integration 就是这个流程。([Cloudflare Docs][7])

---

# 七、配置 wrangler.jsonc

建议：

```jsonc
{
  "$schema": "node_modules/wrangler/config-schema.json",

  "name": "foresightnine-web",

  "main": "src/worker/index.ts",

  "compatibility_date": "2026-08-31",

  "compatibility_flags": [
    "nodejs_compat"
  ],

  "assets": {
    "directory": "./dist",
    "binding": "ASSETS",
    "not_found_handling": "single-page-application",

    "run_worker_first": [
      "/api/*"
    ]
  },

  "secrets": {
    "required": [
      "BETTER_AUTH_SECRET",
      "GOOGLE_CLIENT_ID",
      "GOOGLE_CLIENT_SECRET",
      "GITHUB_CLIENT_ID",
      "GITHUB_CLIENT_SECRET"
    ]
  }
}
```

这里最重要的是：

```json
"run_worker_first": [
  "/api/*"
]
```

意味着：

```text
/                    → Static Assets
/login               → Static Assets
/dashboard           → Static Assets
/assets/app.js       → Static Assets

/api/auth/*           → Worker
/api/user             → Worker
/api/market           → Worker
```

这样不会让每个：

```text
JS
CSS
PNG
favicon
```

都浪费一次 Worker quota。

Cloudflare Static Assets 当前：

> 静态文件请求免费且不限量。

([Cloudflare Docs][8])

---

# 八、创建 D1

本机项目根目录：

```bash
npx wrangler login
```

浏览器授权 Cloudflare。

然后：

```bash
npx wrangler d1 create foresightnine-db
```

Cloudflare 会返回类似：

```text
database_name = "foresightnine-db"
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

官方 `wrangler d1 create` 就是当前推荐方式。([Cloudflare Docs][9])

把它放入：

```jsonc
{
  "d1_databases": [
    {
      "binding": "DB",
      "database_name": "foresightnine-db",
      "database_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
  ]
}
```

最终：

```jsonc
{
  "$schema": "node_modules/wrangler/config-schema.json",

  "name": "foresightnine-web",
  "main": "src/worker/index.ts",

  "compatibility_date": "2026-08-31",
  "compatibility_flags": ["nodejs_compat"],

  "assets": {
    "directory": "./dist",
    "binding": "ASSETS",
    "not_found_handling": "single-page-application",
    "run_worker_first": ["/api/*"]
  },

  "d1_databases": [
    {
      "binding": "DB",
      "database_name": "foresightnine-db",
      "database_id": "你的-ID"
    }
  ],

  "secrets": {
    "required": [
      "BETTER_AUTH_SECRET",
      "GOOGLE_CLIENT_ID",
      "GOOGLE_CLIENT_SECRET",
      "GITHUB_CLIENT_ID",
      "GITHUB_CLIENT_SECRET"
    ]
  }
}
```

D1 是 SQLite 语义的 Cloudflare serverless SQL database。([Cloudflare Docs][10])

---

# 九、Better Auth 配置

例如：

```text
src/worker/auth.ts
```

核心结构：

```ts
import { betterAuth } from "better-auth";

interface Env {
  DB: D1Database;

  BETTER_AUTH_SECRET: string;

  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;

  GITHUB_CLIENT_ID: string;
  GITHUB_CLIENT_SECRET: string;
}

export function createAuth(env: Env, origin: string) {
  return betterAuth({
    appName: "ForesightNine",

    database: env.DB,

    baseURL: origin,

    basePath: "/api/auth",

    secret: env.BETTER_AUTH_SECRET,

    trustedOrigins: [origin],

    socialProviders: {
      google: {
        clientId: env.GOOGLE_CLIENT_ID,
        clientSecret: env.GOOGLE_CLIENT_SECRET
      },

      github: {
        clientId: env.GITHUB_CLIENT_ID,
        clientSecret: env.GITHUB_CLIENT_SECRET
      }
    }
  });
}
```

Better Auth 已经支持直接：

```ts
database: env.DB
```

使用 Cloudflare D1 binding。([Better Auth][2])

---

# 十、Worker 路由

例如：

```text
src/worker/index.ts
```

```ts
import { Hono } from "hono";
import { createAuth } from "./auth";

type Env = {
  DB: D1Database;

  BETTER_AUTH_SECRET: string;

  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;

  GITHUB_CLIENT_ID: string;
  GITHUB_CLIENT_SECRET: string;
};

const app = new Hono<{ Bindings: Env }>();

app.on(["GET", "POST"], "/api/auth/*", async (c) => {
  const origin = new URL(c.req.url).origin;

  const auth = createAuth(c.env, origin);

  return auth.handler(c.req.raw);
});

app.get("/api/health", (c) => {
  return c.json({
    ok: true
  });
});

export default app;
```

于是：

```text
/api/auth/sign-in/social
/api/auth/callback/google
/api/auth/callback/github
/api/auth/get-session
/api/auth/sign-out
```

全部运行在 Cloudflare Worker。

Better Auth 官方标准 handler 就挂载在：

```text
/api/auth/*
```

这一类 catch-all route。([Better Auth][11])

---

# 十一、生成 BETTER_AUTH_SECRET

这个是你自己的 session/auth 加密密钥。

不要随便写：

```text
123456
foresightnine
secret
```

Windows 上直接：

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"
```

它会生成类似：

```text
rFH1AjH1J8...............
```

复制下来。

Better Auth 要求至少 32 字节高熵 secret。([Better Auth][11])

---

# 十二、Google OAuth

这里是唯一需要离开 Cloudflare 控制台一次的地方之一。

Google 本身必须给你的 App 发：

```text
Client ID
Client Secret
```

## 12.1 建 Google Cloud Project

进入：

```text
Google Cloud Console
→ Google Auth Platform
```

创建项目：

```text
ForesightNine
```

---

## 12.2 Branding

进入：

```text
Google Auth Platform
→ Branding
```

填写：

```text
App name
ForesightNine

Support email
你的邮箱

Homepage
https://app.example.com

Privacy Policy
https://app.example.com/privacy

Terms
https://app.example.com/terms
```

Authorized domain：

```text
example.com
```

Google 现在要求 OAuth 应用配置 consent/branding，并且公开应用相关链接必须位于你控制的 authorized domain。([Google for Developers][12])

所以建议你提前至少有：

```text
/privacy
/terms
```

两个页面。

---

# 十三、Google OAuth Client

进入：

```text
Google Auth Platform
→ Clients
→ Create Client
```

选择：

```text
Web application
```

名称：

```text
ForesightNine Web
```

Authorized JavaScript origin：

```text
https://app.example.com
```

Authorized redirect URI：

```text
https://app.example.com/api/auth/callback/google
```

注意：

```text
❌ https://app.example.com/api/auth/callback
❌ https://app.example.com/
❌ http://app.example.com/...
```

必须和代码生成的 callback：

```text
https://app.example.com/api/auth/callback/google
```

完全对应。

Google 对 Web OAuth redirect URI 要求 HTTPS，并要求和客户端配置匹配。([Google for Developers][13])

创建以后得到：

```text
GOOGLE_CLIENT_ID

xxx.apps.googleusercontent.com
```

以及：

```text
GOOGLE_CLIENT_SECRET

GOCSPX-xxxxxxxx
```

---

# 十四、Google scopes

如果你现在只是：

> “用 Google 登录”

那么只要基本身份：

```text
openid
email
profile
```

不要顺手申请：

```text
Google Drive
Google Calendar
Gmail
Contacts
```

否则会进入更复杂的 sensitive/restricted scope verification。

Google 也建议只申请完成功能所需的最小 scopes。([Google for Developers][14])

你的目标只是知道：

```text
Google user id
name
email
avatar
```

即可。

---

# 十五、Google Production

测试阶段可以先：

```text
Testing
```

测试完成再：

```text
Publish
```

公开给所有 Google 用户。

正式公开时，Google 可能要求验证：

```text
域名所有权
App name
Homepage
Privacy Policy
Support email
Branding
```

这是**验证，不是收费**。Google 当前对 External + Published + branding 的应用有相应 verification 要求。([Google for Developers][15])

---

# 十六、GitHub OAuth

GitHub：

```text
Settings
→ Developer settings
→ OAuth Apps
→ New OAuth App
```

不是：

```text
GitHub App
```

对于单纯：

```text
Sign in with GitHub
```

OAuth App 最简单。

填写：

```text
Application name:
ForesightNine

Homepage URL:
https://app.example.com
```

Authorization callback URL：

```text
https://app.example.com/api/auth/callback/github
```

生成：

```text
GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET
```

Better Auth 对 GitHub provider 直接支持。([Better Auth][16])

---

# 十七、GitHub 现在有一个 2026 新坑

截至 2026-08，GitHub OAuth App 新增了 callback wildcard matching 控制。

如果看到：

```text
Enable wildcard matching
```

**关掉。**

使用 exact：

```text
https://app.example.com/api/auth/callback/github
```

GitHub 官方明确警告 wildcard callback 会扩大 OAuth code 被劫持的攻击面。([GitHub Docs][17])

---

# 十八、GitHub Email

Better Auth 特别提醒 GitHub 登录要能访问用户 email。

因为 GitHub 用户可能设置：

```text
Hide my email
```

导致：

```json
{
  "email": null
}
```

Better Auth 的 GitHub 文档要求确保能够访问 `user:email`。([Better Auth][16])

所以最终要测试：

```text
公开邮箱用户
+
隐藏邮箱用户
```

两种。

---

# 十九、Secrets 全部放 Cloudflare

千万不要：

```ts
const GOOGLE_CLIENT_SECRET = "GOCSPX-xxxxx";
```

也不要：

```text
.env → git push
```

进入：

```text
Cloudflare Dashboard
→ Workers & Pages
→ foresightnine-web
→ Settings
→ Variables and Secrets
→ Add
```

类型选择：

```text
Secret
```

分别添加：

```text
BETTER_AUTH_SECRET

GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET

GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET
```

Cloudflare 会加密保存 Secret，之后 Dashboard 也不会再显示明文。([Cloudflare Docs][18])

---

# 二十、本地 secrets

本地开发创建：

```text
.dev.vars
```

例如：

```dotenv
BETTER_AUTH_SECRET="xxxx"

GOOGLE_CLIENT_ID="xxxx"
GOOGLE_CLIENT_SECRET="xxxx"

GITHUB_CLIENT_ID="xxxx"
GITHUB_CLIENT_SECRET="xxxx"
```

`.gitignore`：

```gitignore
.dev.vars
.dev.vars.*
.env
.env.*
```

Cloudflare 官方明确要求 `.dev.vars/.env` 不要提交 Git。([Cloudflare Docs][19])

---

# 二十一、D1 Auth 表初始化

Better Auth 至少需要：

```text
user
session
account
verification
```

([Better Auth][20])

由于 D1 binding 只能在 Worker runtime 中使用，Better Auth 对 Cloudflare D1 推荐使用 programmatic migration。([Better Auth][21])

最简单是创建一次性的：

```text
/api/internal/migrate
```

并用 secret 保护。

增加 Secret：

```text
MIGRATION_SECRET
```

代码概念：

```ts
import { getMigrations } from "better-auth/db/migration";

app.post("/api/internal/migrate", async (c) => {
  const authHeader = c.req.header("Authorization");

  if (
    authHeader !==
    `Bearer ${c.env.MIGRATION_SECRET}`
  ) {
    return c.text("Forbidden", 403);
  }

  const origin = new URL(c.req.url).origin;
  const auth = createAuth(c.env, origin);

  const {
    toBeCreated,
    toBeAdded,
    runMigrations
  } = await getMigrations(auth.options);

  await runMigrations();

  return c.json({
    ok: true,
    created: toBeCreated.map(x => x.table),
    added: toBeAdded.map(x => x.table)
  });
});
```

先部署一次。

然后：

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_MIGRATION_SECRET" \
  https://app.example.com/api/internal/migrate
```

完成之后：

### 立即删除：

```text
/api/internal/migrate
```

以及：

```text
MIGRATION_SECRET
```

不要把 migration endpoint 永久暴露公网。

Better Auth 的 D1 示例也明确要求生产环境必须保护或删除 migration endpoint。([Fossies][22])

---

# 二十二、前端登录

React：

```ts
import { createAuthClient } from "better-auth/react";

export const authClient = createAuthClient();
```

因为前端和 API 在同一个：

```text
app.example.com
```

所以不需要：

```text
CORS
api.example.com
cross-domain cookies
```

这是我强烈建议同域的原因。

Google：

```ts
await authClient.signIn.social({
  provider: "google",
  callbackURL: "/"
});
```

GitHub：

```ts
await authClient.signIn.social({
  provider: "github",
  callbackURL: "/"
});
```

Better Auth 官方就是通过 `signIn.social()` 调 social provider。([Better Auth][23])

---

# 二十三、登录按钮

前端：

```tsx
<button
  onClick={() =>
    authClient.signIn.social({
      provider: "google",
      callbackURL: "/"
    })
  }
>
  Continue with Google
</button>

<button
  onClick={() =>
    authClient.signIn.social({
      provider: "github",
      callbackURL: "/"
    })
  }
>
  Continue with GitHub
</button>
```

---

# 二十四、读取当前用户

React：

```tsx
const {
  data: session,
  isPending
} = authClient.useSession();
```

最终：

```text
session.user.id
session.user.email
session.user.name
session.user.image
```

不要：

```text
localStorage.setItem("user", ...)
```

让 Auth session 自己控制。

---

# 二十五、绑定正式域名

Worker 部署正常后：

```text
Cloudflare
→ Workers & Pages
→ foresightnine-web
→ Settings
→ Domains & Routes
→ Add
→ Custom Domain
```

填：

```text
app.example.com
```

然后：

```text
Add Custom Domain
```

Cloudflare 自动：

```text
DNS
+
TLS Certificate
+
Worker mapping
```

无需：

```text
Nginx
Caddy
Certbot
Let's Encrypt
VPS
```

([Cloudflare Docs][4])

---

# 二十六、也可以把 Custom Domain 写进代码配置

最终稳定后，我反而推荐配置即代码：

```jsonc
{
  "routes": [
    {
      "pattern": "app.example.com",
      "custom_domain": true
    }
  ]
}
```

这样：

```text
Git repo
```

就是 Cloudflare Worker 配置的 source of truth。

Cloudflare 官方也推荐 Wrangler config 作为 Worker 配置 source of truth。([Cloudflare Docs][6])

---

# 二十七、GitHub 自动部署

现在应该形成：

```text
local
 ↓
git commit
 ↓
git push origin main
 ↓
GitHub
 ↓
Cloudflare Workers Builds
 ↓
npm install
 ↓
npm run build
 ↓
Worker + Static Assets
 ↓
app.example.com
```

Cloudflare：

```text
Worker
→ Settings
→ Builds
```

确认：

```text
Git Repository: connected

Production branch:
main
```

当前 Free Workers Builds 是：

```text
3000 build minutes/month
1 concurrent build
20 minute timeout
```

([Cloudflare Docs][24])

个人项目基本绰绰有余。

---

# 二十八、推荐 Build 配置

假设：

```json
{
  "scripts": {
    "build": "vite build",
    "deploy": "wrangler deploy"
  }
}
```

Cloudflare：

```text
Build command:
npm run build

Deploy command:
npx wrangler deploy
```

如果 repository 本身已有正确：

```text
wrangler.jsonc
```

Cloudflare 可以自动识别配置。

---

# 二十九、不要把 secrets 配到 GitHub Actions

因为已经使用：

```text
Cloudflare Git integration
```

所以不需要：

```text
CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
GitHub Actions deploy
```

保持：

```text
GitHub → source code

Cloudflare → runtime secrets
```

就行。

---

# 三十、Cloudflare 安全配置

这几个免费功能建议开。

## HTTPS

Cloudflare 自动处理。

建议：

```text
SSL/TLS
→ Edge Certificates
```

开启：

```text
Always Use HTTPS
```

---

## Security headers

Worker response 建议：

```text
Strict-Transport-Security
X-Content-Type-Options
Referrer-Policy
Content-Security-Policy
```

但 Google OAuth popup / GIS 如果以后启用 One Tap，需要正确配置 CSP；Google 官方对此有专门要求。([Google for Developers][12])

普通 redirect OAuth 没那么麻烦。

---

# 三十一、Turnstile 建议开

特别是你的：

```text
/api/chat
/api/generate
/api/analyze
```

这种会触发昂贵 AI API 的 endpoint。

Cloudflare Turnstile：

```text
Cloudflare
→ Turnstile
→ Add Widget
```

domain：

```text
app.example.com
```

会得到：

```text
SITE_KEY
SECRET_KEY
```

前端拿 SITE_KEY。

Worker 拿 SECRET_KEY。

Turnstile Free 目前支持：

```text
20 widgets/account
10 hostnames/widget
unlimited challenges
```

也就是正常 App 基本完全免费。([Cloudflare Docs][25])

---

# 三十二、不要用 Access 保护普通 App

不要：

```text
app.example.com
    ↓
Cloudflare Access
```

给公开用户。

应该：

```text
app.example.com
    ↓
Google/GitHub OAuth
```

Access 留给：

```text
admin.example.com
```

例如：

```text
admin.example.com
   ↓
Cloudflare Access
   ↓
只有你
```

这样 Access 免费 50-user 限额完全不会影响你的普通用户。

---

# 三十三、最重要的免费优化

最终 routing：

```text
app.example.com/
                     ↓
Static Asset
FREE

app.example.com/dashboard
                     ↓
SPA index.html
FREE

app.example.com/assets/app.js
                     ↓
Static Asset CDN
FREE

app.example.com/api/auth/*
                     ↓
Worker

app.example.com/api/market/*
                     ↓
Worker

app.example.com/api/user/*
                     ↓
Worker
```

不要：

```json
"run_worker_first": true
```

否则用户每刷新一个 HTML 页面都要消耗 Worker request。

应该：

```json
"run_worker_first": [
  "/api/*"
]
```

这是这套免费架构里非常重要的一步。([Cloudflare Docs][8])

---

# 三十四、注意 Worker Free CPU

这里有一个必须说清楚的限制。

Workers Free：

```text
CPU <= 10ms / invocation
```

等待：

```text
Google
GitHub
D1
fetch()
```

的网络时间不算 CPU。

但：

```text
大量 JSON parsing
复杂 SSR
本地 ML
PDF parsing
复杂加密计算
```

会计算 CPU。

Cloudflare 官方甚至指出：

> 普通 Worker 平均约 2.2ms，但 auth/SSR/重型请求可能达到 10–20ms。

([Cloudflare Docs][26])

所以这套 V1：

```text
React static frontend
+
轻量 Auth Worker
+
D1
+
proxy/fetch
```

比较适合 Free。

不要在 Worker 内：

```text
跑模型
做复杂 dataframe
批量因子计算
解析大 PDF
```

---

# 三十五、建议最终域名结构

我会直接定成：

```text
example.com
    landing page

app.example.com
    正式用户 App
    Google OAuth
    GitHub OAuth
    Worker
    D1

admin.example.com
    可选
    Cloudflare Access
```

不建议再拆：

```text
api.example.com
```

至少 V1 不要。

保持：

```text
app.example.com/api/*
```

最大的好处是：

```text
没有 CORS
没有 third-party cookie
OAuth callback 简单
session cookie 简单
前后端统一 origin
```

---

# 三十六、最终你需要准备的东西

最终实际上只有这些配置值：

```text
Cloudflare
────────────────────────────
DOMAIN=example.com
APP_DOMAIN=app.example.com
D1_DATABASE_ID=...
BETTER_AUTH_SECRET=...

Google
────────────────────────────
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

GitHub
────────────────────────────
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
```

OAuth callback 固定：

```text
Google:
https://app.example.com/api/auth/callback/google

GitHub:
https://app.example.com/api/auth/callback/github
```

---

# 三十七、完整验收顺序

我建议严格按这个顺序做，不要同时乱配：

```text
① Cloudflare domain Active

        ↓

② GitHub repo → Cloudflare Worker

        ↓

③ workers.dev 能打开前端

        ↓

④ 创建 D1

        ↓

⑤ D1 binding = DB

        ↓

⑥ Worker /api/health 正常

        ↓

⑦ 配 Better Auth

        ↓

⑧ 创建 Google OAuth

        ↓

⑨ 创建 GitHub OAuth

        ↓

⑩ Cloudflare Secrets 配好

        ↓

⑪ Better Auth migration

        ↓

⑫ Google login 成功

        ↓

⑬ GitHub login 成功

        ↓

⑭ session 刷新页面仍存在

        ↓

⑮ logout 正常

        ↓

⑯ 绑定 app.example.com

        ↓

⑰ 把 Google/GitHub callback
   改成正式域名(foresightnine.com)

        ↓

⑱ 删除 migration endpoint

        ↓

⑲ push main

        ↓

⑳ GitHub → Cloudflare 自动部署
```

最终你应该能够做到：

```text
用户访问
https://app.example.com

↓

点击
Continue with Google
或者
Continue with GitHub

↓

OAuth provider

↓

https://app.example.com/api/auth/callback/...

↓

Cloudflare Worker

↓

Better Auth

↓

Cloudflare D1

↓

创建/读取 user + account + session

↓

返回 App

↓

浏览器持有安全 session cookie
```

而整个 Web 基础设施：

```text
DNS             Cloudflare
HTTPS           Cloudflare
CDN             Cloudflare
Frontend        Workers Static Assets
Backend API     Workers
Database        D1
Secrets         Workers Secrets
Deploy          Workers Builds
Git             GitHub
Google Login    Google OAuth
GitHub Login    GitHub OAuth
Bot Protection  Turnstile
```

**不需要 VPS、不需要 Nginx、不需要自己续 SSL、不需要 Redis、不需要 Supabase、不需要 Auth0、不需要 Clerk。**

唯一要持续注意的是 Workers Free 的 **10ms CPU/请求**；所以认证和 API Worker 要保持轻量。初期小规模 App 完全值得先按这套做，真碰到 CPU 限制时再决定是否升级，而不是一开始就付费。([Cloudflare Docs][26])

[1]: https://developers.cloudflare.com/workers/platform/pricing/?utm_source=chatgpt.com "Pricing · Cloudflare Workers docs"
[2]: https://better-auth.com/blog/1-5?utm_source=chatgpt.com "Better Auth 1.5"
[3]: https://developers.cloudflare.com/d1/platform/pricing/?utm_source=chatgpt.com "Pricing · Cloudflare D1 docs"
[4]: https://developers.cloudflare.com/workers/configuration/routing/custom-domains/?utm_source=chatgpt.com "Custom Domains · Cloudflare Workers docs"
[5]: https://developers.cloudflare.com/workers/static-assets/?utm_source=chatgpt.com "Static Assets · Cloudflare Workers docs"
[6]: https://developers.cloudflare.com/workers/wrangler/configuration/?utm_source=chatgpt.com "Configuration - Wrangler · Cloudflare Workers docs"
[7]: https://developers.cloudflare.com/workers/ci-cd/builds/git-integration/github-integration/?utm_source=chatgpt.com "GitHub integration · Cloudflare Workers docs"
[8]: https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/?utm_source=chatgpt.com "Billing and Limitations · Cloudflare Workers docs"
[9]: https://developers.cloudflare.com/d1/wrangler-commands/?utm_source=chatgpt.com "Wrangler commands · Cloudflare D1 docs"
[10]: https://developers.cloudflare.com/d1/?utm_source=chatgpt.com "Overview · Cloudflare D1 docs"
[11]: https://better-auth.com/docs/installation?utm_source=chatgpt.com "Installation | Better Auth"
[12]: https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid?utm_source=chatgpt.com "Setup | Web guides"
[13]: https://developers.google.com/identity/protocols/oauth2/web-server?utm_source=chatgpt.com "Using OAuth 2.0 for Web Server Applications | Authorization"
[14]: https://developers.google.com/identity/protocols/oauth2/scopes?utm_source=chatgpt.com "OAuth 2.0 Scopes for Google APIs"
[15]: https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification?utm_source=chatgpt.com "Submit for brand verification | App verification to use ..."
[16]: https://better-auth.com/docs/authentication/github?utm_source=chatgpt.com "GitHub | Better Auth"
[17]: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps?utm_source=chatgpt.com "Authorizing OAuth apps - GitHub Docs"
[18]: https://developers.cloudflare.com/workers/configuration/secrets/ "Secrets · Cloudflare Workers docs"
[19]: https://developers.cloudflare.com/workers/configuration/secrets/?utm_source=chatgpt.com "Secrets · Cloudflare Workers docs"
[20]: https://better-auth.com/docs/concepts/database "Database | Better Auth"
[21]: https://better-auth.com/docs/beta/concepts/database?utm_source=chatgpt.com "v1.8 (Beta) - Database | Better Auth"
[22]: https://fossies.org/linux/better-auth/docs/content/docs/concepts/database.mdx?utm_source=chatgpt.com "Better Auth: docs/content/docs/concepts/database.mdx | Fossies"
[23]: https://better-auth.com/docs/1.6/basic-usage?utm_source=chatgpt.com "v1.6 - Basic Usage | Better Auth"
[24]: https://developers.cloudflare.com/workers/ci-cd/builds/limits-and-pricing/?utm_source=chatgpt.com "Limits & pricing · Cloudflare Workers docs"
[25]: https://developers.cloudflare.com/turnstile/plans/?utm_source=chatgpt.com "Cloudflare Turnstile plans · Cloudflare Turnstile docs"
[26]: https://developers.cloudflare.com/workers/platform/limits/?utm_source=chatgpt.com "Limits · Cloudflare Workers docs"
