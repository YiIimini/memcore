window.__ModuleLoader__.load({
  id: '@local/memory-ui',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useState, useEffect, useRef, useCallback } = React;

    const PORTS = [8790, 8791, 8792, 8793, 8794, 8795];
    let API = 'http://127.0.0.1:8790';

    async function probe() {
      for (const p of PORTS) {
        try {
          const r = await fetch('http://127.0.0.1:' + p + '/api/graph?limit=1');
          if (r.ok) { API = 'http://127.0.0.1:' + p; return p; }
        } catch (e) { /* try next */ }
      }
      throw new Error('no panel');
    }
    const CORE = '__me__';
    const KIND = {
      core: '#8b9dff', identity: '#f472b6', preference: '#a78bfa', decision: '#60a5fa',
      fact: '#34d399', project: '#fbbf24', artifact: '#fb923c', insight: '#22d3ee',
      episode: '#94a3b8', todo: '#ef4444', note: '#64748b',
    };

    // ---- layout: core pinned at origin, others in a jittered ring, then relaxed ----
    function layoutOf(data) {
      const nodes = (data.nodes || []).filter(n => n.id !== CORE).map(n => ({ ...n, x: 0, y: 0, vx: 0, vy: 0 }));
      const core = { id: CORE, title: '我', kind: 'core', x: 0, y: 0, fixed: true, preview: '', tags: [] };
      const all = [core, ...nodes];
      const N = nodes.length || 1;
      nodes.forEach((n, i) => {
        const ring = 1 + (i % 3) * 0.55;
        const a = (i / N) * Math.PI * 2 + (i % 7) * 0.09;
        const r = 150 * ring + (n.deg || 0) * 3;
        n.x = Math.cos(a) * r; n.y = Math.sin(a) * r;
      });
      const edges = (data.edges || []).filter(e => e.source !== CORE && e.target !== CORE);
      const byId = new Map(all.map(n => [n.id, n]));
      const links = edges.map(e => ({ a: byId.get(e.source), b: byId.get(e.target), rel: e.rel }))
        .filter(e => e.a && e.b);
      for (let t = 0; t < 90; t++) {
        for (let i = 0; i < nodes.length; i++) {
          const a = nodes[i];
          for (let j = i + 1; j < nodes.length; j++) {
            const b = nodes[j];
            let dx = b.x - a.x, dy = b.y - a.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
            if (d2 > 90000) continue;
            const f = 2600 / d2;
            const d = Math.sqrt(d2);
            a.vx -= dx / d * f; a.vy -= dy / d * f;
            b.vx += dx / d * f; b.vy += dy / d * f;
          }
          // pull to its ring radius (orbiting the core)
          const rr = Math.hypot(a.x, a.y) || 1;
          const target = 170 + (a.deg || 0) * 4;
          a.vx += (a.x / rr) * (target - rr) * 0.01;
          a.vy += (a.y / rr) * (target - rr) * 0.01;
        }
        links.forEach(e => {
          const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
          const d = Math.max(1, Math.hypot(dx, dy));
          const f = (d - 90) * 0.0016;
          e.a.vx += dx / d * f; e.a.vy += dy / d * f;
          e.b.vx -= dx / d * f; e.b.vy -= dy / d * f;
        });
        nodes.forEach(n => {
          n.vx *= 0.82; n.vy *= 0.82;
          n.x += n.vx; n.y += n.vy;
          const d = Math.hypot(n.x, n.y);
          if (d > 640) { n.x *= 640 / d; n.y *= 640 / d; }
        });
      }
      return { all, links };
    }

    function MemorySection() {
      const [data, setData] = useState(null);
      const [err, setErr] = useState('');
      const [q, setQ] = useState('');
      const [sel, setSel] = useState(null);
      const [hover, setHover] = useState(null);
      const [view, setView] = useState({ x: 0, y: 0, k: 1 });
      const log = useRef({ all: [], links: [] });
      const cvRef = useRef(null);
      const boxRef = useRef(null);

      const load = useCallback(async () => {
        try {
          await probe();
          const r = await fetch(API + '/api/graph?limit=900');
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const d = await r.json();
          log.current = layoutOf(d);
          setData(d); setErr('');
        } catch (e) {
          setErr('记忆面板服务未运行（' + e.message + '）。可在终端执行 mem serve，或双击桌面「记忆面板」。');
        }
      }, []);
      useEffect(() => { load(); }, [load]);

      const nodeAt = useCallback((sx, sy) => {
        const cv = cvRef.current; if (!cv) return null;
        const w = cv.clientWidth, h = cv.clientHeight;
        const wx = (sx - w / 2 - view.x) / view.k, wy = (sy - h / 2 - view.y) / view.k;
        let best = null, bd = 16 / view.k;
        log.current.all.forEach(n => {
          const d = Math.hypot(n.x - wx, n.y - wy);
          if (d < bd) { bd = d; best = n; }
        });
        return best;
      }, [view]);

      const draw = useCallback(() => {
        const cv = cvRef.current; if (!cv) return;
        const dpr = window.devicePixelRatio || 1;
        const w = cv.clientWidth, h = cv.clientHeight;
        if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
        const g = cv.getContext('2d');
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, w, h);
        g.save(); g.translate(w / 2 + view.x, h / 2 + view.y); g.scale(view.k, view.k);
        const s = sel ? sel.id : null;
        const near = new Set(s ? [s] : []);
        if (s) log.current.links.forEach(e => {
          if (e.a.id === s) near.add(e.b.id);
          if (e.b.id === s) near.add(e.a.id);
        });
        log.current.links.forEach(e => {
          const lit = !s || (near.has(e.a.id) && near.has(e.b.id));
          g.strokeStyle = e.rel === 'supersedes' ? '#ef4444' : '#60a5fa';
          g.globalAlpha = lit ? (s ? .5 : .16) : .04;
          g.lineWidth = 1 / view.k;
          g.beginPath(); g.moveTo(e.a.x, e.a.y); g.lineTo(e.b.x, e.b.y); g.stroke();
        });
        const ql = q.trim().toLowerCase();
        log.current.all.forEach(n => {
          const hit = !ql || (n.title || '').toLowerCase().includes(ql);
          const lit = (!s || near.has(n.id)) && hit;
          const isCore = n.id === CORE;
          const r = isCore ? 13 : 3 + Math.sqrt(n.deg || 0) * 1.1 + (n.salience || 0) * 1.6;
          g.globalAlpha = lit ? 1 : .12;
          g.beginPath(); g.arc(n.x, n.y, r, 0, Math.PI * 2);
          g.fillStyle = isCore ? '#dfe6ff' : (KIND[n.kind] || '#64748b');
          g.fill();
          if (isCore || n.id === s) {
            g.strokeStyle = '#8b9dff'; g.lineWidth = 1.4 / view.k;
            g.beginPath(); g.arc(n.x, n.y, r + 4 / view.k, 0, Math.PI * 2); g.stroke();
          }
        });
        g.globalAlpha = 1;
        g.restore();
      }, [view, sel, q]);

      useEffect(() => { draw(); }, [draw, data]);

      const onMove = ev => {
        const rect = cvRef.current.getBoundingClientRect();
        setHover(nodeAt(ev.clientX - rect.left, ev.clientY - rect.top));
      };

      const act = async name => {
        if (!sel) return;
        if (name === 'forget' && !window.confirm('删除 ' + sel.id + '？')) return;
        try {
          await fetch(API + '/api/action', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, id: sel.id }),
          });
          setSel(null); await load();
        } catch (e) { setErr('操作失败：' + e.message); }
      };

      const wrap = { display: 'flex', flexDirection: 'column', gap: 10, padding: '4px 2px', color: 'inherit', height: '100%' };
      const bar = { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' };
      const btn = { padding: '4px 10px', borderRadius: 7, border: '1px solid color-mix(in srgb, currentColor 22%, transparent)', background: 'transparent', color: 'inherit', cursor: 'pointer', fontSize: 12 };
      const card = { border: '1px solid color-mix(in srgb, currentColor 16%, transparent)', borderRadius: 10, padding: 10, fontSize: 12, lineHeight: 1.55 };

      if (err) {
        return h('div', { style: wrap },
          h('div', { style: { fontSize: 13, opacity: .85 } }, err),
          h('div', null, h('button', { style: btn, onClick: load }, '重试')));
      }
      if (!data) return h('div', { style: { padding: 8, opacity: .6, fontSize: 12 } }, '加载记忆图谱…');

      const nodes = data.nodes.filter(n => n.id !== CORE);
      const kinds = {};
      nodes.forEach(n => { kinds[n.kind] = (kinds[n.kind] || 0) + 1; });

      return h('div', { style: wrap },
        h('div', { style: bar },
          h('strong', { style: { fontSize: 13 } }, '记忆图谱'),
          h('span', { style: { opacity: .6, fontSize: 11.5 } }, nodes.length + ' 条 · ' + data.edges.length + ' 条关联'),
          h('input', {
            value: q, onChange: e => setQ(e.target.value), placeholder: '搜索标题…',
            style: { marginLeft: 'auto', padding: '4px 8px', borderRadius: 7, border: '1px solid color-mix(in srgb, currentColor 20%, transparent)', background: 'transparent', color: 'inherit', fontSize: 12, width: 160 },
          }),
          h('button', { style: btn, onClick: load }, '刷新'),
          h('a', { href: API + '/', target: '_blank', rel: 'noreferrer', style: { ...btn, textDecoration: 'none' } }, '完整面板'),
        ),
        h('div', { style: { display: 'flex', gap: 12, minHeight: 340, flex: 1 } },
          h('div', { ref: boxRef, style: { position: 'relative', flex: 1, minWidth: 260, height: 380, borderRadius: 10, border: '1px solid color-mix(in srgb, currentColor 14%, transparent)', overflow: 'hidden', cursor: 'pointer' } },
            h('canvas', {
              ref: cvRef, style: { width: '100%', height: '100%', display: 'block' },
              onMouseMove: onMove, onMouseLeave: () => setHover(null),
              onClick: ev => {
                const rect = ev.currentTarget.getBoundingClientRect();
                const n = nodeAt(ev.clientX - rect.left, ev.clientY - rect.top);
                setSel(n && n.id !== CORE ? n : null);
              },
            }),
            hover ? h('div', {
              style: {
                position: 'absolute', left: 8, bottom: 8, maxWidth: '92%', padding: '6px 8px', borderRadius: 8,
                background: 'color-mix(in srgb, currentColor 8%, transparent)', fontSize: 11.5, pointerEvents: 'none',
              },
            }, hover.title + (hover.kind ? ' · ' + hover.kind : '')) : null,
          ),
          h('div', { style: { width: 230, display: 'flex', flexDirection: 'column', gap: 8 } },
            sel ? h('div', { style: card },
              h('div', { style: { fontWeight: 600, marginBottom: 4 } }, sel.title),
              h('div', { style: { opacity: .65, fontSize: 11 } },
                sel.kind + ' · ' + (sel.project || '—') + ' · 连接 ' + (sel.deg || 0)),
              h('div', { style: { margin: '6px 0', maxHeight: 150, overflow: 'auto', opacity: .9 } },
                (sel.preview || '').slice(0, 400)),
              h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
                h('button', { style: btn, onClick: () => act('pin') }, '置顶'),
                h('button', { style: btn, onClick: () => act('retire') }, '标记失效'),
                h('button', { style: btn, onClick: () => act('forget') }, '删除'),
              ),
            ) : h('div', { style: { ...card, opacity: .7 } },
              h('div', { style: { fontWeight: 600, marginBottom: 6 } }, '我 · 核心'),
              h('div', null, '所有记忆都连回这里。点击任意记忆点查看内容并操作。'),
              h('div', { style: { marginTop: 8, display: 'flex', gap: 4, flexWrap: 'wrap' } },
                Object.keys(kinds).sort((a, b) => kinds[b] - kinds[a]).map(k =>
                  h('span', { key: k, style: { fontSize: 10.5, padding: '1px 6px', borderRadius: 999, background: 'color-mix(in srgb, ' + (KIND[k] || '#64748b') + ' 22%, transparent)' } },
                    k + ' ' + kinds[k]))),
              h('button', { style: { ...btn, marginTop: 10 }, onClick: load }, '重新加载'),
            ),
          ),
        ),
      );
    }

    class Boundary extends React.Component {
      constructor(props) { super(props); this.state = { err: null }; }
      static getDerivedStateFromError(err) { return { err }; }
      render() {
        if (this.state.err) {
          return h('div', { style: { padding: 12, fontSize: 12, opacity: .85 } },
            '记忆视图加载失败：' + String(this.state.err && this.state.err.message || this.state.err));
        }
        return this.props.children;
      }
    }

    function Section() { return h(Boundary, null, h(MemorySection)); }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('settings.section', () => ctx.slots.register({
          name: 'settings.section', id: 'memory', order: 40, label: '记忆',
        }, Section));
      },
    };
  },
});
