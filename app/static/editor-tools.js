(function (root) {
    const WORD_RE = /\S+/g;

    function wordCount(text) {
        if (!text) return 0;
        const matches = String(text).match(WORD_RE);
        return matches ? matches.length : 0;
    }

    function readingTimeMinutes(text, wpm) {
        const words = wordCount(text);
        if (words <= 0) return 0;
        const pace = wpm > 0 ? wpm : 260;
        return Math.max(1, Math.ceil(words / pace));
    }

    function escapeRegex(value) {
        return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    function findSpans(text, query, options) {
        const opts = options || {};
        const sourceText = text || '';
        if (!query) return { spans: [], error: null };
        let source = opts.regex ? query : escapeRegex(query);
        if (opts.wholeWord) {
            source = opts.regex ? `\\b(?:${query})\\b` : `\\b${source}\\b`;
        }
        const flags = opts.caseSensitive ? 'g' : 'gi';
        let pattern;
        try {
            pattern = new RegExp(source, flags);
        } catch (err) {
            return { spans: [], error: 'Invalid regex: ' + err.message };
        }
        const spans = [];
        let match;
        while ((match = pattern.exec(sourceText))) {
            if (!match[0].length) {
                pattern.lastIndex += 1;
                continue;
            }
            spans.push([match.index, match.index + match[0].length]);
        }
        return { spans, error: null };
    }

    function replaceSpans(text, spans, replacement) {
        let out = text || '';
        const repl = replacement == null ? '' : String(replacement);
        for (let i = spans.length - 1; i >= 0; i -= 1) {
            const start = spans[i][0];
            const end = spans[i][1];
            out = out.slice(0, start) + repl + out.slice(end);
        }
        return out;
    }

    function replaceAll(text, query, replacement, options) {
        const found = findSpans(text, query, options);
        if (found.error) return { text: text || '', count: 0, error: found.error };
        return {
            text: replaceSpans(text, found.spans, replacement),
            count: found.spans.length,
            error: null,
        };
    }

    root.WriterTools = {
        wordCount,
        readingTimeMinutes,
        findSpans,
        replaceSpans,
        replaceAll,
    };
}(window));
