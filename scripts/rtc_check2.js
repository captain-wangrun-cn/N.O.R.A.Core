const crypto = require('crypto');
const fs = require('fs');

// 从 debug 文件读 raw，从 config 读 token（都在 HK，这里本地做个等价验证）
// 本地没有这两个文件——改成参数化
const botToken = process.argv[2];
const raw = fs.readFileSync('/tmp/rtc_raw.txt', 'utf8').trim();

const urlParams = new URLSearchParams(raw);
const hash = urlParams.get('hash');
urlParams.delete('hash');
// 注意：URLSearchParams 的 entries() 是插入序，sort() 是对 "k=v" 串排序（官方 JS 样例就是这么写的）
const dataCheckString = [...urlParams.entries()]
  .map(([key, value]) => key + '=' + value)
  .sort()
  .join('\n');

const secretKey = crypto.createHmac('sha256', 'WebAppData').update(botToken).digest();
const computedHash = crypto.createHmac('sha256', secretKey).update(dataCheckString).digest('hex');
console.log('JS official match:', computedHash === hash);
