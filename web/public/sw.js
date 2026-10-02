/* RunOS 最小 Service Worker：只为满足可安装性，不做离线缓存。
   数据正确性优先——一切请求直连本地后端，绝不拦截、绝不改写、绝不缓存；
   后端不在时页面本来就无法使用，假装离线可用只会掩盖问题。 */
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()))
self.addEventListener('fetch', (e) => e.respondWith(fetch(e.request)))
