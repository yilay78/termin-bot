// Service Worker – Berlin Termin Bot PWA
const CACHE = 'termin-bot-v1';
const ASSETS = ['/', '/manifest.json'];

// Kurulum
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(ASSETS))
  );
  self.skipWaiting();
});

// Aktivasyon
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch – önce ağ, hata varsa cache
self.addEventListener('fetch', e => {
  // WebSocket isteklerini atla
  if (e.request.url.includes('/ws')) return;

  e.respondWith(
    fetch(e.request)
      .then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});

// Push bildirimleri
self.addEventListener('push', e => {
  const data = e.data?.json() || {};
  e.waitUntil(
    self.registration.showNotification(data.title || 'Termin Bot', {
      body   : data.body  || 'Neuer Termin verfügbar!',
      icon   : '/icon-192.png',
      badge  : '/icon-192.png',
      vibrate: [200, 100, 200],
      tag    : 'termin-bot',
      actions: [
        { action: 'open',  title: '🗓 Termin buchen' },
        { action: 'close', title: 'Schließen' },
      ],
      data: { url: data.url || '/' },
    })
  );
});

// Bildirime tıklanınca uygulamayı aç
self.addEventListener('notificationclick', e => {
  e.notification.close();
  if (e.action === 'close') return;
  e.waitUntil(
    clients.matchAll({ type: 'window' }).then(list => {
      for (const c of list) {
        if (c.url === '/' && 'focus' in c) return c.focus();
      }
      return clients.openWindow(e.notification.data?.url || '/');
    })
  );
});
