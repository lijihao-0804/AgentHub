// Read-only geometry checks for browser QA. Clipped text and closed details are excluded.
export const measure = () => { const scope = document.querySelector('main'); const root = document.documentElement; const nodes = Array.from(document.querySelectorAll('main *,header *')); const escaped = nodes.filter(e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && r.right > innerWidth + 2 && !e.closest('.data-table,svg'); }).map(e => ({ tag: e.tagName, class: String(e.className), text: e.textContent.slice(0, 60), right: e.getBoundingClientRect().right })); const controls = Array.from(document.querySelectorAll('.topbar button')).filter(e => e.getBoundingClientRect().width > 0); const collisions = []; for (let i = 0; i < controls.length; i++)
    for (let j = i + 1; j < controls.length; j++) {
        const a = controls[i].getBoundingClientRect(), b = controls[j].getBoundingClientRect();
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2)
            collisions.push([controls[i].textContent, controls[j].textContent]);
    } return { url: location.pathname, width: innerWidth, pageWidth: root.scrollWidth, escaped, topbarCollisions: collisions, heading: scope?.querySelector('h1')?.textContent, workbench: Array.from(document.querySelectorAll('.research-workbench > *')).map(e => ({ class: e.className, width: e.getBoundingClientRect().width, col: getComputedStyle(e).gridColumn })) }; };
export const textAudit = () => { let texts = []; for (const e of document.querySelectorAll('main *, .topbar *')) {
    if (e.closest('svg,pre,.sr-only') || (e.closest('details:not([open])') && !e.closest('summary')))
        continue;
    let s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden')
        continue;
    for (const n of e.childNodes) {
        if (n.nodeType !== 3 || !n.textContent.trim())
            continue;
        let range = document.createRange();
        range.selectNodeContents(n);
        for (const raw of range.getClientRects()) {
            let r = { left: raw.left, right: raw.right, top: raw.top, bottom: raw.bottom };
            for (let p = e; p; p = p.parentElement) {
                let z = getComputedStyle(p), b = p.getBoundingClientRect();
                if (z.overflowX !== 'visible') {
                    r.left = Math.max(r.left, b.left);
                    r.right = Math.min(r.right, b.right);
                }
                if (z.overflowY !== 'visible') {
                    r.top = Math.max(r.top, b.top);
                    r.bottom = Math.min(r.bottom, b.bottom);
                }
            }
            if (r.right - r.left > 1 && r.bottom - r.top > 1)
                texts.push({ text: n.textContent.trim().slice(0, 45), parent: e, rect: r });
        }
    }
} let overlaps = []; for (let i = 0; i < texts.length; i++)
    for (let j = i + 1; j < texts.length; j++) {
        let a = texts[i], b = texts[j];
        if (a.parent.closest("main") !== b.parent.closest("main"))
            continue;
        if (a.parent === b.parent || a.parent.contains(b.parent) || b.parent.contains(a.parent))
            continue;
        let x = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
        let y = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
        if (x > 3 && y > 3)
            overlaps.push({ a: a.text, b: b.text, x, y });
    } return { textRects: texts.length, textOverlaps: overlaps.slice(0, 30) }; };
