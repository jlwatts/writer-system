(function (root) {
    function splitLines(text) {
        const source = String(text || '');
        if (!source) return [];
        const lines = [];
        let start = 0;
        for (let i = 0; i < source.length; i += 1) {
            if (source[i] === '\n') {
                lines.push(source.slice(start, i + 1));
                start = i + 1;
            }
        }
        if (start < source.length) lines.push(source.slice(start));
        return lines;
    }

    function normalizeLine(line, ignoreCase, ignoreSpace) {
        let value = String(line || '').replace(/\n$/, '');
        if (ignoreSpace) value = value.replace(/\s+/g, '');
        if (ignoreCase) value = value.toLowerCase();
        return value;
    }

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function buildHunks(left, right, ignoreCase, ignoreSpace) {
        const Diff = root.Diff;
        if (!Diff || !Diff.diffArrays) {
            return { hunks: [], identical: left === right, error: 'Diff library failed to load' };
        }
        const leftLines = splitLines(left);
        const rightLines = splitLines(right);
        const leftNorm = leftLines.map((line) => normalizeLine(line, ignoreCase, ignoreSpace));
        const rightNorm = rightLines.map((line) => normalizeLine(line, ignoreCase, ignoreSpace));
        const tokens = Diff.diffArrays(leftNorm, rightNorm);
        const parts = [];
        let li = 0;
        let ri = 0;
        tokens.forEach((token) => {
            const count = token.count || 0;
            if (token.added) {
                parts.push({ added: true, value: rightLines.slice(ri, ri + count).join('') });
                ri += count;
            } else if (token.removed) {
                parts.push({ removed: true, value: leftLines.slice(li, li + count).join('') });
                li += count;
            } else {
                parts.push({
                    value: leftLines.slice(li, li + count).join(''),
                    rightValue: rightLines.slice(ri, ri + count).join(''),
                });
                li += count;
                ri += count;
            }
        });

        const hunks = [];
        let lOff = 0;
        let rOff = 0;
        let pending = null;
        function flush() {
            if (pending) {
                hunks.push(pending);
                pending = null;
            }
        }
        parts.forEach((part) => {
            const leftVal = part.removed || !part.added ? (part.value || '') : '';
            const rightVal = part.added ? (part.value || '') : (part.rightValue || (!part.removed ? part.value : '') || '');
            if (part.added) {
                if (!pending) {
                    pending = { leftStart: lOff, leftEnd: lOff, rightStart: rOff, rightEnd: rOff, leftText: '', rightText: '' };
                }
                pending.rightEnd += rightVal.length;
                pending.rightText += rightVal;
                rOff += rightVal.length;
            } else if (part.removed) {
                if (!pending) {
                    pending = { leftStart: lOff, leftEnd: lOff, rightStart: rOff, rightEnd: rOff, leftText: '', rightText: '' };
                }
                pending.leftEnd += leftVal.length;
                pending.leftText += leftVal;
                lOff += leftVal.length;
            } else {
                flush();
                lOff += leftVal.length;
                rOff += rightVal.length;
            }
        });
        flush();
        return { parts, hunks, identical: hunks.length === 0 };
    }

    function highlightSide(parts, side) {
        let html = '';
        (parts || []).forEach((part) => {
            if (side === 'left' && part.added) return;
            if (side === 'right' && part.removed) return;
            const kind = part.added ? 'add' : part.removed ? 'del' : 'eq';
            const val = side === 'right' && !part.added && !part.removed
                ? (part.rightValue || part.value || '')
                : (part.value || '');
            html += `<span class="diff-${kind}">${escapeHtml(val)}</span>`;
        });
        return html || '<span class="diff-eq"></span>';
    }

    function charCompare(a, b, ignoreCase, ignoreSpace) {
        const Diff = root.Diff;
        const leftText = ignoreSpace ? String(a || '').replace(/\s+/g, ' ').trim() : String(a || '');
        const rightText = ignoreSpace ? String(b || '').replace(/\s+/g, ' ').trim() : String(b || '');
        if (!Diff || !Diff.diffChars) {
            return `<div class="diff-line-row">Left: ${escapeHtml(a || '(empty)')}</div>`
                + `<div class="diff-line-row">Right: ${escapeHtml(b || '(empty)')}</div>`;
        }
        const parts = Diff.diffChars(leftText, rightText, { ignoreCase: !!ignoreCase });
        let top = '';
        let bottom = '';
        parts.forEach((part) => {
            const safe = escapeHtml(part.value || '');
            if (part.added) bottom += `<span class="diff-add">${safe}</span>`;
            else if (part.removed) top += `<span class="diff-del">${safe}</span>`;
            else {
                top += `<span class="diff-eq">${safe}</span>`;
                bottom += `<span class="diff-eq">${safe}</span>`;
            }
        });
        return `<div class="diff-line-row">Left: ${top || '<span class="diff-eq">(empty)</span>'}</div>`
            + `<div class="diff-line-row">Right: ${bottom || '<span class="diff-eq">(empty)</span>'}</div>`;
    }

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
        buildHunks,
        highlightSide,
        charCompare,
        mergeMarkdown,
    };
}(window));
