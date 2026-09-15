import { fetchAutotradeOverview, type AutotradeOverview } from './client';

export const AUTOTRADE_OBSERVER_REFRESH_MS = 5_000;

export type AutotradeObserverState = {
    overview: AutotradeOverview | null;
    error: string | null;
    lastUpdatedAt: number | null;
    stale: boolean;
};

type Listener = (state: AutotradeObserverState) => void;
type FetchOverview = () => Promise<AutotradeOverview>;
type EventSourceLike = {
    addEventListener: (type: string, listener: () => void) => void;
    close: () => void;
};
type EventSourceFactory = () => EventSourceLike | null;

const initialState: AutotradeObserverState = {
    overview: null,
    error: null,
    lastUpdatedAt: null,
    stale: false,
};

function createAutotradeEventSource(): EventSourceLike | null {
    if (typeof EventSource === 'undefined') return null;
    return new EventSource('/api/v1/autotrade/events');
}

/**
 * Shared observer-only refresh loop for all Autotrade panels.
 *
 * A workspace can contain several Autotrade panels.  They must see the same
 * durable projection without multiplying requests, and a temporary Gateway
 * failure must not erase the last valid evidence or look like an empty order
 * book.  This store deliberately owns read requests only.
 */
export class AutotradeObserverStore {
    private state: AutotradeObserverState = initialState;
    private readonly listeners = new Set<Listener>();
    private timer: ReturnType<typeof setInterval> | null = null;
    private refreshInFlight: Promise<void> | null = null;
    private refreshRequested = false;
    private eventSource: EventSourceLike | null = null;

    constructor(
        private readonly fetchOverview: FetchOverview = fetchAutotradeOverview,
        private readonly refreshMs = AUTOTRADE_OBSERVER_REFRESH_MS,
        private readonly eventSourceFactory: EventSourceFactory = createAutotradeEventSource,
    ) {}

    subscribe(listener: Listener): () => void {
        this.listeners.add(listener);
        listener(this.state);
        if (this.listeners.size === 1) this.start();
        return () => {
            this.listeners.delete(listener);
            if (this.listeners.size === 0) this.stop();
        };
    }

    snapshot(): AutotradeObserverState {
        return this.state;
    }

    refresh(): Promise<void> {
        if (this.refreshInFlight) {
            // An SSE event can arrive while a snapshot began before its
            // durable write.  Queue exactly one follow-up read rather than
            // losing that edge or starting unbounded concurrent requests.
            this.refreshRequested = true;
            return this.refreshInFlight;
        }
        this.refreshInFlight = this.fetchOverview()
            .then((overview) => {
                this.state = {
                    overview,
                    error: null,
                    lastUpdatedAt: Date.now(),
                    stale: false,
                };
                this.notify();
            })
            .catch(() => {
                this.state = {
                    ...this.state,
                    error: 'Gateway 更新暫時失敗',
                    // Preserve durable observations rather than reporting a
                    // false empty state after a transient read failure.
                    stale: this.state.overview !== null,
                };
                this.notify();
            })
            .finally(() => {
                this.refreshInFlight = null;
                if (this.refreshRequested) {
                    this.refreshRequested = false;
                    void this.refresh();
                }
            });
        return this.refreshInFlight;
    }

    private start(): void {
        void this.refresh();
        this.timer = setInterval(() => { void this.refresh(); }, this.refreshMs);
        this.eventSource = this.eventSourceFactory();
        this.eventSource?.addEventListener('autotrade-order', () => {
            // The event has no authority to mutate a local order.  It only
            // tells the shared observer to reload the durable read model.
            void this.refresh();
        });
    }

    private stop(): void {
        if (this.timer !== null) clearInterval(this.timer);
        this.timer = null;
        this.eventSource?.close();
        this.eventSource = null;
    }

    private notify(): void {
        for (const listener of this.listeners) listener(this.state);
    }
}

export const autotradeObserverStore = new AutotradeObserverStore();
