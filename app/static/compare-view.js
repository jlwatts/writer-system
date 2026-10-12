import { EditorState } from "@codemirror/state";
import { EditorView, drawSelection, keymap, lineNumbers } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { Change, MergeView, goToNextChunk, goToPreviousChunk } from "@codemirror/merge";
import { oneDark } from "@codemirror/theme-one-dark";

const sessions = new Map();

function splitLines(text) {
    const source = String(text || "");
    if (!source) return [];
    const lines = [];
    let start = 0;
    for (let i = 0; i < source.length; i += 1) {
        if (source[i] === "\n") {
            lines.push(source.slice(start, i + 1));
            start = i + 1;
        }
    }
    if (start < source.length) lines.push(source.slice(start));
    return lines;
}

function normalizeLine(line, ignoreCase, ignoreSpace) {
    let value = String(line || "").replace(/\n$/, "");
    if (ignoreSpace) value = value.replace(/\s+/g, "");
    if (ignoreCase) value = value.toLowerCase();
    return value;
}

function charChanges(leftText, rightText, leftStart, rightStart, ignoreCase, ignoreSpace) {
    const Diff = window.Diff;
    const parts = Diff.diffChars(leftText, rightText, { ignoreCase: !!ignoreCase });
    const changes = [];
    let left = leftStart;
    let right = rightStart;
    let pending = null;
    function flush() {
        if (pending && (pending.toA > pending.fromA || pending.toB > pending.fromB)) {
            changes.push(new Change(pending.fromA, pending.toA, pending.fromB, pending.toB));
        }
        pending = null;
    }
    parts.forEach((part) => {
        const value = part.value || "";
        const skip = ignoreSpace && /^\s+$/.test(value);
        if (part.added) {
            if (!skip) {
                if (!pending) pending = { fromA: left, toA: left, fromB: right, toB: right };
                pending.toB += value.length;
            }
            right += value.length;
        } else if (part.removed) {
            if (!skip) {
                if (!pending) pending = { fromA: left, toA: left, fromB: right, toB: right };
                pending.toA += value.length;
            }
            left += value.length;
        } else {
            flush();
            left += value.length;
            right += value.length;
        }
    });
    flush();
    return changes;
}

function ignoreAwareDiff(left, right, ignoreCase, ignoreSpace) {
    const Diff = window.Diff;
    if (!Diff || !Diff.diffArrays || !Diff.diffChars) {
        throw new Error("Diff library failed to load");
    }
    const leftLines = splitLines(left);
    const rightLines = splitLines(right);
    const tokens = Diff.diffArrays(
        leftLines.map((line) => normalizeLine(line, ignoreCase, ignoreSpace)),
        rightLines.map((line) => normalizeLine(line, ignoreCase, ignoreSpace)),
    );
    const changes = [];
    let leftIndex = 0;
    let rightIndex = 0;
    let leftOffset = 0;
    let rightOffset = 0;
    let pending = null;
    function flushDeletion() {
        if (!pending) return;
        changes.push(new Change(pending.start, pending.start + pending.text.length, rightOffset, rightOffset));
        pending = null;
    }
    tokens.forEach((token) => {
        const count = token.count || 0;
        if (token.removed) {
            const text = leftLines.slice(leftIndex, leftIndex + count).join("");
            if (!pending) pending = { start: leftOffset, text: "" };
            pending.text += text;
            leftOffset += text.length;
            leftIndex += count;
            return;
        }
        if (token.added) {
            const text = rightLines.slice(rightIndex, rightIndex + count).join("");
            const removed = pending ? pending.text : "";
            const removedStart = pending ? pending.start : leftOffset;
            pending = null;
            changes.push(...charChanges(removed, text, removedStart, rightOffset, ignoreCase, ignoreSpace));
            rightOffset += text.length;
            rightIndex += count;
            return;
        }
        flushDeletion();
        leftOffset += leftLines.slice(leftIndex, leftIndex + count).join("").length;
        rightOffset += rightLines.slice(rightIndex, rightIndex + count).join("").length;
        leftIndex += count;
        rightIndex += count;
    });
    flushDeletion();
    return changes;
}

function diffConfig(ignoreCase, ignoreSpace) {
    if (!ignoreCase && !ignoreSpace) return { scanLimit: 500 };
    return {
        scanLimit: 500,
        override(left, right) {
            return ignoreAwareDiff(left, right, ignoreCase, ignoreSpace);
        },
    };
}

function titleBar(labels, gutter) {
    const bar = document.createElement("div");
    const columns = gutter ? " has-gutter" : labels.length > 1 ? " is-pair" : "";
    bar.className = "diff-titles" + columns;
    labels.forEach((text, index) => {
        if (gutter && index === 1) {
            const gap = document.createElement("div");
            gap.className = "diff-title-gap";
            bar.appendChild(gap);
        }
        const label = document.createElement("div");
        label.className = "diff-pane-label";
        label.textContent = text || "";
        label.title = text || "";
        bar.appendChild(label);
    });
    return bar;
}

function editorExtensions(readOnly, onChange) {
    const extensions = [
        lineNumbers(),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        drawSelection(),
        EditorView.lineWrapping,
        oneDark,
        EditorView.contentAttributes.of({ spellcheck: "false" }),
        EditorView.theme({
            "&": { fontSize: "12px" },
            ".cm-scroller": { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" },
        }),
    ];
    if (onChange) {
        extensions.push(EditorView.updateListener.of((update) => {
            if (update.docChanged) onChange();
        }));
    }
    if (readOnly) {
        extensions.push(EditorState.readOnly.of(true), EditorView.editable.of(false));
    }
    return extensions;
}

function arrowIcon(direction) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("d", direction === "rtl" ? "M10.5 3.5 5.5 8l5 4.5" : "M5.5 3.5 10.5 8l-5 4.5");
    svg.appendChild(path);
    return svg;
}

function copyChunk(merge, index, ontoA) {
    const chunk = merge.chunks[index];
    if (!chunk) return;
    const source = ontoA ? merge.b : merge.a;
    const dest = ontoA ? merge.a : merge.b;
    const srcFrom = ontoA ? chunk.fromB : chunk.fromA;
    const srcTo = ontoA ? chunk.toB : chunk.toA;
    const destFrom = ontoA ? chunk.fromA : chunk.fromB;
    const destTo = ontoA ? chunk.toA : chunk.toB;
    let insert = source.state.sliceDoc(srcFrom, Math.max(srcFrom, srcTo - 1));
    if (srcFrom !== srcTo && destTo <= dest.state.doc.length) insert += source.state.lineBreak;
    dest.dispatch({
        changes: { from: destFrom, to: Math.min(dest.state.doc.length, destTo), insert },
        userEvent: "revert",
    });
}

function renderArrowPair(getMerge) {
    const wrap = document.createElement("div");
    wrap.className = "diff-arrow-pair";
    const toLeft = document.createElement("button");
    toLeft.type = "button";
    toLeft.title = "Copy right to left";
    toLeft.setAttribute("aria-label", "Copy right to left");
    toLeft.appendChild(arrowIcon("rtl"));
    toLeft.addEventListener("mousedown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        const merge = getMerge();
        if (merge) copyChunk(merge, Number(wrap.dataset.chunk), true);
    });
    const toRight = document.createElement("button");
    toRight.type = "button";
    toRight.title = "Copy left to right";
    toRight.setAttribute("aria-label", "Copy left to right");
    toRight.appendChild(arrowIcon("ltr"));
    wrap.append(toLeft, toRight);
    return wrap;
}

function destroy(parent) {
    const session = sessions.get(parent);
    if (session) {
        session.view.destroy();
        sessions.delete(parent);
    }
    if (parent) parent.replaceChildren();
}

function mountPair(parent, options) {
    destroy(parent);
    const readOnly = !!options.readOnly;
    const showRevert = !readOnly && options.revert !== false;
    const onChange = typeof options.onChange === "function" ? options.onChange : null;
    parent.appendChild(titleBar([options.aLabel, options.bLabel], showRevert));
    let merge = null;
    const config = {
        a: {
            doc: options.a || "",
            extensions: editorExtensions(readOnly, onChange),
        },
        b: {
            doc: options.b || "",
            extensions: editorExtensions(readOnly, onChange),
        },
        parent,
        highlightChanges: true,
        gutter: true,
        collapseUnchanged: { margin: 3, minSize: 4 },
        diffConfig: diffConfig(!!options.ignoreCase, !!options.ignoreSpace),
    };
    if (showRevert) {
        config.revertControls = "a-to-b";
        config.renderRevertControl = () => renderArrowPair(() => merge);
    }
    merge = new MergeView(config);
    sessions.set(parent, { kind: "pair", view: merge, options: { ...options, readOnly } });
    return merge.chunks.length;
}

function mountSingle(parent, options) {
    destroy(parent);
    parent.appendChild(titleBar([options.label], false));
    const view = new EditorView({
        parent,
        doc: options.text || "",
        extensions: editorExtensions(options.readOnly !== false, null),
    });
    sessions.set(parent, { kind: "single", view, options });
}

function sessionFor(parent) {
    return sessions.get(parent) || null;
}

function chunkCount(parent) {
    const session = sessionFor(parent);
    if (!session || session.kind !== "pair") return 0;
    return session.view.chunks.length;
}

function text(parent, side) {
    const session = sessionFor(parent);
    if (!session) return "";
    if (session.kind === "single") return session.view.state.doc.toString();
    const editor = side === "b" ? session.view.b : session.view.a;
    return editor.state.doc.toString();
}

function setIgnore(parent, ignoreCase, ignoreSpace) {
    const session = sessionFor(parent);
    if (!session || session.kind !== "pair") return 0;
    const scroll = session.view.dom.scrollTop;
    const next = {
        ...session.options,
        a: session.view.a.state.doc.toString(),
        b: session.view.b.state.doc.toString(),
        ignoreCase: !!ignoreCase,
        ignoreSpace: !!ignoreSpace,
    };
    const count = mountPair(parent, next);
    const mounted = sessionFor(parent);
    if (mounted) mounted.view.dom.scrollTop = scroll;
    return count;
}

function go(parent, direction) {
    const session = sessionFor(parent);
    if (!session || session.kind !== "pair") return false;
    const command = direction < 0 ? goToPreviousChunk : goToNextChunk;
    return command(session.view.a);
}

window.WriterCompare = {
    mountPair,
    mountSingle,
    destroy,
    setIgnore,
    chunkCount,
    text,
    go,
};
