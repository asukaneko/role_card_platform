// Service Worker - PWA离线缓存支持（v3）
// 仅缓存 /static/ 下的静态资源，其他所有请求完全不干预

const STATIC_CACHE_NAME = 'role-card-static-v3';

// 需要预缓存的静态资源（仅限 /static/ 路径）
const urlsToCache = [
  '/static/css/style.css',
  '/static/js/main.js',
  '/static/png/favicon.png'
];

// 判断是否应该被SW处理的静态资源（仅限 /static/ 目录下的文件）
function shouldHandle(url) {
  const path = url.pathname;
  // 只处理 /static/ 路径下的静态资源
  return path.startsWith('/static/');
}

// 安装事件：预缓存核心静态资源
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(STATIC_CACHE_NAME)
      .then(function(cache) {
        return cache.addAll(urlsToCache);
      })
      .then(function() {
        // 立即激活，不等待旧SW退出
        return self.skipWaiting();
      })
  );
});

// 拦截网络请求：只处理 /static/ 静态资源，其余全部放行
self.addEventListener('fetch', function(event) {
  var request = event.request;

  // 非GET请求：完全放行
  if (request.method !== 'GET') {
    return;
  }

  var requestUrl;
  try {
    requestUrl = new URL(request.url);
  } catch (e) {
    return; // URL解析失败也放行
  }

  // 非静态资源路径：完全放行（不拦截、不缓存）
  // 包括: /assets/*, /api/*, 页面HTML等所有非/static/内容
  if (!shouldHandle(requestUrl)) {
    return;
  }

  // 仅对 /static/ 资源使用 StaleWhileRevalidate 策略
  event.respondWith(
    caches.match(request).then(function(cachedResponse) {
      var fetchPromise = fetch(request).then(function(networkResponse) {
        // 成功获取后更新缓存
        if (networkResponse && networkResponse.status === 200) {
          caches.open(STATIC_CACHE_NAME).then(function(cache) {
            cache.put(request, networkResponse.clone());
          });
        }
        return networkResponse;
      }).catch(function() {
        // 网络失败时降级到缓存
        return cachedResponse;
      });

      // 优先返回缓存，后台静默更新
      return cachedResponse || fetchPromise;
    })
  );
});

// 激活事件：清理所有旧版本缓存，立即接管页面
self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(cacheNames) {
      // 删除所有旧版本缓存（保留当前版本）
      return Promise.all(
        cacheNames
          .filter(function(name) {
            return name !== STATIC_CACHE_NAME;
          })
          .map(function(name) {
            return caches.delete(name);
          })
      );
    }).then(function() {
      // 立即接管所有客户端页面
      return self.clients.claim();
    })
  );
});
