const CACHE_NAME = 'grace-radio-v1';
const ASSETS = [
    '/',
    '/static/css/style.css',
    '/static/js/main.js',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    'https://fonts.googleapis.com/css2?family=Outfit:wght@200;400;600&display=swap'
];

self.addEventListener('install', (e) => {
    self.skipWaiting();
    e.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            // We try to cache, but don't fail if some missing
            return cache.addAll(ASSETS).catch(err => console.log('SW cache error:', err));
        })
    );
});

self.addEventListener('activate', (e) => {
    e.waitUntil(clients.claim());
});

self.addEventListener('fetch', (e) => {
    // 1. API calls -> Network Only (never cache)
    if (e.request.url.includes('/api/')) {
        return; // browser default (network)
    }

    // 2. Audio/Media -> Network Only (streaming)
    if (e.request.url.includes('/static/media/')) {
        return;
    }

    // 3. Static Assets -> Cache First, fallback to Network
    e.respondWith(
        caches.match(e.request).then((response) => {
            return response || fetch(e.request);
        })
    );
});
