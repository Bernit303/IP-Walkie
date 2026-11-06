// Minimal pass-through worker: makes the app installable. No caching, so the
// walkie always loads the live page.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
