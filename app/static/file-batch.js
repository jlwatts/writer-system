(function (root) {
    function stripMergeJunk(text) {
        let value = String(text || '');
        value = value.replace(/```[\s\S]*?```/g, '\n');
        value = value.replace(/file_path\s*=[\s\S]*?File updated successfully:[^\n]*/g, '\n');
        value = value.replace(/Your updated Markdown file is ready![\s\S]*?(?=\n#{1,6} )/g, '\n');
        value = value.replace(/Here is the complete text[^\n]*\n+/g, '\n');
        value = value.replace(/!\[MD icon\]\([^)]+\)\s*/gi, '');
        value = value.replace(/^[^\n]*first_year_summary[^\n]*\n(?:MD\n)?(?:Open\n)?/gim, '\n');
        value = value.replace(/^Code output\s*$/gim, '');
        value = value.replace(/^#{1,6}\s*$/gm, '');
        return value;
    }

    function paragraphKey(block) {
        return String(block || '')
            .replace(/[*_>#`]/g, '')
            .replace(/\s+/g, ' ')
            .trim()
            .toLowerCase();
    }

    function splitParagraphs(text) {
        return String(text || '').split(/\n{2,}/).map((block) => block.trim()).filter(Boolean);
    }

    function mergeMarkdown(texts) {
        const seen = new Set();
        const out = [];
        (texts || []).forEach((raw) => {
            const cleaned = stripMergeJunk(raw);
            splitParagraphs(cleaned).forEach((block) => {
                const key = paragraphKey(block);
                if (!key) return;
                const heading = /^#{1,6}\s+\S/.test(block);
                if (seen.has(key) && (heading || key.length >= 50)) return;
                if (heading || key.length >= 50) seen.add(key);
                out.push(block);
            });
        });
        return `${out.join('\n\n').trim()}\n`;
    }

    root.WriterBatch = {
        mergeMarkdown,
    };
}(window));
