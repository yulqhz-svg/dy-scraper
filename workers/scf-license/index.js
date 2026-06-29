const crypto = require('crypto');

const LICENSE_SECRET = process.env.LICENSE_SECRET || '';
const ADMIN_SECRET = process.env.ADMIN_SECRET || '';
const REVOKED_KEYS = (process.env.REVOKED_KEYS || '').split(',').filter(Boolean);
const MAX_DEVICES = parseInt(process.env.ACTIVE_DEVICES || '1');
const EPOCH = new Date('2025-01-01');

// ---- 启动时密钥校验 (CRITICAL: 防止静默密码学失效) ----
if (!LICENSE_SECRET || LICENSE_SECRET.length !== 32) {
  throw new Error('FATAL: LICENSE_SECRET 必须为 32 位 hex 字符串');
}
try {
  Buffer.from(LICENSE_SECRET, 'hex');
} catch (_) {
  throw new Error('FATAL: LICENSE_SECRET 不是合法的 hex 字符串');
}
if (!ADMIN_SECRET || ADMIN_SECRET.length < 16) {
  throw new Error('FATAL: ADMIN_SECRET 必须至少 16 字符');
}

// ---- 速率限制 (防暴力破解) ----
const rateLimitMap = new Map();          // ip -> { count, resetAt }
const RATE_LIMIT_MAX = 20;              // 每个窗口最多 20 次
const RATE_LIMIT_WINDOW_MS = 60_000;    // 窗口 60 秒

function checkRateLimit(ip) {
  const now = Date.now();
  let entry = rateLimitMap.get(ip);
  if (!entry || now > entry.resetAt) {
    entry = { count: 0, resetAt: now + RATE_LIMIT_WINDOW_MS };
    rateLimitMap.set(ip, entry);
  }
  entry.count++;
  return entry.count <= RATE_LIMIT_MAX;
}

// 定期清理过期条目（每 5 分钟）
setInterval(() => {
  const now = Date.now();
  for (const [ip, entry] of rateLimitMap) {
    if (now > entry.resetAt) rateLimitMap.delete(ip);
  }
}, 300_000).unref();

// ---- 设备激活追踪 (Vulnerability: 同一 Key 多设备) ----
const deviceRegistry = new Map(); // licenseKey -> Set of machineCodes

// ---- 工具函数 ----
function hmacSign(message, secret) {
  return crypto.createHmac('sha256', Buffer.from(secret, 'hex')).update(message).digest('hex');
}

function extractHex(licenseKey) {
  return licenseKey.replace(/^DYSCRAPER-|-/g, '').toLowerCase();
}

function parseLicenseKey(hexKey) {
  const sigHex = hexKey.substring(0, 8);
  const xoredHex = hexKey.substring(8, 16);
  const sigNum = parseInt(sigHex, 16);
  const xoredNum = parseInt(xoredHex, 16);
  const expiryDays = (xoredNum ^ sigNum) >>> 0;
  const expiryDate = new Date(EPOCH.getTime() + expiryDays * 86400000);
  return { sigHex, expiryDays, expiryDate };
}

// 统一的验证函数，返回具体原因（内部使用）
function validateInternal(machineCode, licenseKey) {
  if (!machineCode || !licenseKey) return { valid: false, reason: '参数缺失' };
  if (REVOKED_KEYS.includes(licenseKey)) return { valid: false, reason: 'REVOKED' };

  const hexKey = extractHex(licenseKey);
  if (!/^[0-9a-f]{16}$/.test(hexKey)) return { valid: false, reason: 'FORMAT' };

  const { sigHex, expiryDays, expiryDate } = parseLicenseKey(hexKey);
  if (Date.now() > expiryDate.getTime()) return { valid: false, reason: 'EXPIRED' };

  const expectedSig = hmacSign(`${machineCode}:vip:${expiryDays}`, LICENSE_SECRET).substring(0, 8);
  if (sigHex !== expectedSig) return { valid: false, reason: 'SIGNATURE' };

  return { valid: true, expiry: expiryDate.getTime() };
}

// 对外返回的错误消息（模糊化，不泄露验证细节）
const EXTERNAL_REASONS = {
  'REVOKED': 'License 无效',
  'FORMAT': 'License 无效',
  'EXPIRED': 'License 无效',
  'SIGNATURE': 'License 无效',
  '参数缺失': '参数缺失',
};

function publicReason(internal) {
  return EXTERNAL_REASONS[internal] || internal || '验证失败';
}

// ---- 激活（含设备绑定） ----
function activate(machineCode, licenseKey) {
  const result = validateInternal(machineCode, licenseKey);
  if (!result.valid) {
    return { success: false, error: publicReason(result.reason) };
  }

  const devices = deviceRegistry.get(licenseKey) || new Set();
  if (devices.size >= MAX_DEVICES && !devices.has(machineCode)) {
    return { success: false, error: '该 License 已绑定其他设备，如需换绑请联系管理员' };
  }
  devices.add(machineCode);
  deviceRegistry.set(licenseKey, devices);

  return { success: true, expiry: result.expiry };
}

// ---- 主处理函数 ----
exports.main_handler = async (event) => {
  const { path, httpMethod, headers, body: rawBody } = event;
  const body = rawBody ? JSON.parse(rawBody) : {};

  const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Content-Type': 'application/json',
  };

  if (httpMethod === 'OPTIONS') return { statusCode: 200, headers: corsHeaders, body: '' };

  // 健康检查不校验速率限制
  if (path === '/api/health') {
    return {
      statusCode: 200,
      headers: corsHeaders,
      body: JSON.stringify({ status: 'ok' }),
    };
  }

  // ---- 速率限制 (MEDIUM-1 fix: 使用 API 网关真实 IP) ----
  const clientIp =
    (event.requestContext && event.requestContext.identity && event.requestContext.identity.sourceIp) ||
    (headers && (headers['x-forwarded-for'] || headers['x-real-ip'])) ||
    'unknown';
  if (!checkRateLimit(clientIp)) {
    return {
      statusCode: 429,
      headers: corsHeaders,
      body: JSON.stringify({ error: '请求过于频繁，请稍后再试' }),
    };
  }

  // ---- 请求体大小校验 (防恶意大包) ----
  if (rawBody && rawBody.length > 4096) {
    return {
      statusCode: 413,
      headers: corsHeaders,
      body: JSON.stringify({ error: '请求体过大' }),
    };
  }

  try {
    let result;
    switch (path) {
      case '/api/verify': {
        const r = validateInternal(body.machineCode, body.licenseKey);
        if (r.valid) {
          result = { valid: true, expiry: r.expiry };
        } else {
          result = { valid: false, reason: publicReason(r.reason) };
        }
        break;
      }
      case '/api/activate':
        result = activate(body.machineCode, body.licenseKey);
        break;
      case '/api/renew': {
        const r = validateInternal(body.machineCode, body.licenseKey);
        if (r.valid) {
          result = { valid: true, expiry: r.expiry };
        } else {
          result = { valid: false, reason: publicReason(r.reason) };
        }
        break;
      }
      case '/api/generate-key':
        if (!ADMIN_SECRET || body.adminToken !== ADMIN_SECRET) {
          result = { error: '未授权' };
        } else if (!body.machineCode || !body.expiryDays || body.expiryDays <= 0) {
          result = { error: '参数无效' };
        } else {
          const days = Math.ceil((Date.now() - EPOCH.getTime()) / 86400000) + body.expiryDays;
          const message = `${body.machineCode}:vip:${days}`;
          const sigHex = hmacSign(message, LICENSE_SECRET).substring(0, 8);
          const sigNum = parseInt(sigHex, 16);
          const xoredHex = ((days ^ sigNum) >>> 0).toString(16).padStart(8, '0');
          result = {
            key: `DYSCRAPER-${(sigHex + xoredHex).match(/.{1,4}/g).join('-')}`.toUpperCase(),
            expiryDays: body.expiryDays,
          };
        }
        break;
      case '/api/revoke':
        if (!ADMIN_SECRET || body.adminToken !== ADMIN_SECRET) {
          result = { error: '未授权' };
        } else if (!body.licenseKey) {
          result = { error: '参数无效' };
        } else {
          REVOKED_KEYS.push(body.licenseKey);
          deviceRegistry.delete(body.licenseKey);
          result = {
            revoked: true,
            note: '已从内存撤销。如需持久化，请更新 SCF 环境变量 REVOKED_KEYS 并重新部署。',
          };
        }
        break;
      default:
        result = { error: 'Not Found' };
    }
    return { statusCode: 200, headers: corsHeaders, body: JSON.stringify(result) };
  } catch (e) {
    // 不泄露内部错误详情
    return {
      statusCode: 500,
      headers: corsHeaders,
      body: JSON.stringify({ error: '服务器内部错误' }),
    };
  }
};
