const DB_NAME = "intelliscan-graphs";
const DB_VERSION = 1;

function openStore(mode) {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("graphs")) {
        request.result.createObjectStore("graphs");
      }
    };
    request.onsuccess = () => {
      const db = request.result;
      const tx = db.transaction("graphs", mode);
      resolve(tx.objectStore("graphs"));
    };
    request.onerror = () => reject(request.error);
  });
}

export async function saveToIDB(key, value) {
  const store = await openStore("readwrite");
  return new Promise((resolve, reject) => {
    const req = store.put(value, key);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

export async function getFromIDB(key) {
  const store = await openStore("readonly");
  return new Promise((resolve, reject) => {
    const req = store.get(key);
    req.onsuccess = () => resolve(req.result ?? null);
    req.onerror = () => reject(req.error);
  });
}

export async function deleteIDB(key) {
  const store = await openStore("readwrite");
  return new Promise((resolve, reject) => {
    const req = store.delete(key);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}