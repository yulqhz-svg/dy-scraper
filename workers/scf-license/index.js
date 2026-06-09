const crypto = require('crypto');

const LICENSE_SECRET = process.env.LICENSE_SECRET || '';
const ADMIN_SECRET = process.env.ADMIN_SECRET || '';
const REVOKED_KEYS = (process.env.REVOKED_KEYS || '').split(',').filter(Boolean);
const EPOCH = new Date('2025-01-01');

// 设备绑定说明：
// Key 的 HMAC 签名为 hmac({machineCode}:vip:{days})，机器码已编入签名。
// 因此一个 Key 只能用在一台机器上——换电脑必然验签失败，必须找管理员重新生成。
// 撤销直接更新环境变量 REVOKED_KEYS 并重新部署即可。

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

function validate(machineCode, licenseKey) {
  if (!machineCode || !licenseKey) return { valid: false, reason: '参数缺失' };
  if (REVOKED_KEYS.includes(licenseKey)) return { valid: false, reason: '已撤销' };

  const hexKey = extractHex(licenseKey);
  if (!/^[0-9a-f]{16}$/.test(hexKey)) return { valid: false, reason: '格式错误' };

  const { sigHex, expiryDays, expiryDate } = parseLicenseKey(hexKey);
  if (Date.now() > expiryDate.getTime()) return { valid: false, reason: '已过期' };

  const expectedSig = hmacSign(`${machineCode}:vip:${expiryDays}`, LICENSE_SECRET).substring(0, 8);
  if (sigHex !== expectedSig) return { valid: false, reason: '签名无效（Key 与设备不匹配）' };

  return { valid: true, expiry: expiryDate.getTime() };
}

exports.main_handler = async (event) => {
  const { path, httpMethod, body: rawBody } = event;
  const body = rawBody ? JSON.parse(rawBody) : {};

  const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Content-Type': 'application/json'
  };

  if (httpMethod === 'OPTIONS') return { statusCode: 200, headers: corsHeaders, body: '' };

  try {
    let result;
    switch (path) {
      case '/api/health':
        result = { status: 'ok' };
        break;
      case '/api/verify':
        result = validate(body.machineCode, body.licenseKey);
        break;
      case '/api/activate': {
        // activate 与 verify 逻辑相同——HMAC 签名已保证设备绑定
        const r = validate(body.machineCode, body.licenseKey);
        result = r.valid ? { success: true, expiry: r.expiry } : { success: false, error: r.reason };
        break;
      }
      case '/api/renew':
        result = validate(body.machineCode, body.licenseKey);
        break;
      case '/api/generate-key':
        if (body.adminToken !== ADMIN_SECRET) {
          result = { error: '未授权' };
        } else {
          const days = Math.ceil((Date.now() - EPOCH.getTime()) / 86400000) + body.expiryDays;
          const message = `${body.machineCode}:vip:${days}`;
          const sigHex = hmacSign(message, LICENSE_SECRET).substring(0, 8);
          const sigNum = parseInt(sigHex, 16);
          const xoredHex = ((days ^ sigNum) >>> 0).toString(16).padStart(8, '0');
          result = { key: `DYSCRAPER-${(sigHex + xoredHex).match(/.{1,4}/g).join('-')}`.toUpperCase() };
        }
        break;
      case '/api/revoke':
        if (body.adminToken !== ADMIN_SECRET) {
          result = { error: '未授权' };
        } else {
          // 提示：撤销需更新 SCF 环境变量 REVOKED_KEYS 并重新部署才能持久化
          REVOKED_KEYS.push(body.licenseKey);
          result = { revoked: true, note: '请同时更新环境变量 REVOKED_KEYS 并重新部署以持久化撤销' };
        }
        break;
      default:
        result = { error: 'Not Found' };
    }
    return { statusCode: 200, headers: corsHeaders, body: JSON.stringify(result) };
  } catch (e) {
    return { statusCode: 500, headers: corsHeaders, body: JSON.stringify({ error: e.message }) };
  }
};
