(function (root) {
    function isIPhone(env) {
        var ua = env.userAgent || '';
        if (/iPad/.test(ua)) return false;
        if (/iPhone|iPod/.test(ua)) return true;
        var shortSide = Math.min(env.screenWidth, env.screenHeight);
        return env.maxTouchPoints > 1 && /Macintosh/.test(ua) && shortSide > 0 && shortSide <= 500;
    }

    function sendByDoubleEnter(env) {
        if (isIPhone(env)) return false;
        return !env.landscape || env.coarsePointer;
    }

    function doubleEnterSends(value, env) {
        var text = String(value || '');
        return sendByDoubleEnter(env) && /\n\n$/.test(text);
    }

    function composerPlaceholder(channel, env) {
        if (channel === 'writing' && env.awaiting) return 'Answer to continue...';
        if (sendByDoubleEnter(env) && channel === 'todos') return 'Ask Guppy… (double-tap return to send)';
        if (sendByDoubleEnter(env)) return 'Ask AI or instruct edits (double-tap return to send)…';
        if (isIPhone(env) && !env.landscape && channel === 'todos') return 'Ask Guppy…';
        if (isIPhone(env) && !env.landscape) return 'Ask AI or instruct edits…';
        if (channel === 'todos') return 'Ask Guppy… (Ctrl+Enter to send)';
        return 'Ask AI or instruct edits (Ctrl+Enter to send)…';
    }

    root.WriterComposer = {
        isIPhone: isIPhone,
        sendByDoubleEnter: sendByDoubleEnter,
        doubleEnterSends: doubleEnterSends,
        composerPlaceholder: composerPlaceholder,
    };
}(typeof window !== 'undefined' ? window : this));
