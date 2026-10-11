/**
 * Lets Autotrade panels switch the whole workspace to a named preset.
 *
 * The main window registers its own preset loader; panels in a pop-out window
 * have none, so they hide the switch buttons instead of failing.
 */
type PresetLoader = (name: string) => void;

let loader: PresetLoader | null = null;

export function registerPresetLoader(fn: PresetLoader): () => void {
    loader = fn;
    return () => {
        if (loader === fn) loader = null;
    };
}

export function canLoadPreset(): boolean {
    return loader !== null;
}

export function loadWorkspacePreset(name: string): boolean {
    if (!loader) return false;
    loader(name);
    return true;
}
