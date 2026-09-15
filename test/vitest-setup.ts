/**
 * Vitest runs in Node, while much of the Web UI correctly relies on the
 * browser Storage API.  Some Node launchers predefine `localStorage` but do
 * not supply its methods (for example when their backing file is unavailable).
 * Supply a process-local implementation only in that incomplete case.
 */
function memoryStorage(): Storage {
    const values = new Map<string, string>();
    return {
        get length() { return values.size; },
        clear: () => values.clear(),
        getItem: (key) => values.get(key) ?? null,
        key: (index) => [...values.keys()][index] ?? null,
        removeItem: (key) => { values.delete(key); },
        setItem: (key, value) => { values.set(key, String(value)); },
    };
}

const existing = globalThis.localStorage as Partial<Storage> | undefined;
if (!existing || typeof existing.getItem !== 'function' || typeof existing.setItem !== 'function') {
    Object.defineProperty(globalThis, 'localStorage', {
        configurable: true,
        value: memoryStorage(),
        writable: true,
    });
}
