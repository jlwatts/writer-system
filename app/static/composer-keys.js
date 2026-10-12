(function (root) {
    function shiftEnterSends(key, shiftKey, ctrlKey, metaKey, altKey) {
        if (key === 'Enter' && shiftKey && !ctrlKey && !metaKey && !altKey) return true;
        return false;
    }

    function composerPlaceholder(channel, env) {
        if (channel === 'writing' && env.awaiting) return 'Answer to continue...';
        if (channel === 'todos') return 'Ask Guppy… (Shift+Enter to send)';
        return 'Ask AI or instruct edits (Shift+Enter to send)…';
    }

    root.WriterComposer = {
        shiftEnterSends: shiftEnterSends,
        composerPlaceholder: composerPlaceholder,
    };
}(typeof window !== 'undefined' ? window : this));
