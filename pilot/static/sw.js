// Minimal service worker — required for PWA install prompt.
// No caching: all requests go to the network unchanged.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
