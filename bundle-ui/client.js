window.__ModuleLoader__.load({
  id: '@local/memory-ui',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useState, useEffect, useRef } = React;

    const PORTS = [8790, 8791, 8792, 8793, 8794, 8795];
    const btn = {
      padding: '8px 12px', borderRadius: 8, border: '1px solid #272727', background: 'rgba(255,255,255,.06)',
      color: 'inherit', fontSize: 14, lineHeight: '20px', cursor: 'pointer',
      transition: 'background-color 300ms cubic-bezier(.22,1,.36,1)',
    };

    function MemorySection() {
      const [src, setSrc] = useState('');
      const [failed, setFailed] = useState(false);
      const frame = useRef(null);

      useEffect(() => {
        let alive = true;
        (async () => {
          const pickBg = () => {
            let el = document.body;
            while (el) {
              const c = getComputedStyle(el).backgroundColor;
              if (c && c !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(c)) return c;
              el = el.parentElement;
            }
            return '';
          };
          const bg = pickBg();
          const fg = getComputedStyle(document.body).color || '';
          const rgb = (bg.match(/\d+/g) || []).slice(0, 3).map(Number);
          const dark = rgb.length === 3 ? (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) < 128
                                        : matchMedia('(prefers-color-scheme: dark)').matches;
          const q = '&bg=' + encodeURIComponent(bg || (dark ? '#181818' : '#ffffff')) +
                    '&fg=' + encodeURIComponent(fg || (dark ? '#F2F2F4' : '#18181B'));
          for (const p of PORTS) {
            try {
              const r = await fetch('http://127.0.0.1:' + p + '/api/graph?limit=1');
              if (r.ok) { if (alive) setSrc('http://127.0.0.1:' + p + '/?embed=1' + q); return; }
            } catch (e) { /* next port */ }
          }
          if (alive) setFailed(true);
        })();
        return () => { alive = false; };
      }, []);

      const send = (type) => {
        const f = frame.current;
        if (f && f.contentWindow) f.contentWindow.postMessage({ type }, '*');
      };

      const bar = { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' };
      const title = { fontSize: 14, lineHeight: '20px', fontWeight: 600, marginRight: 'auto' };

      if (failed) {
        return h('div', { style: { padding: 16, fontSize: 14, lineHeight: '20px' } },
          '记忆面板服务没有运行。打开终端执行 mem serve，或双击桌面的「记忆面板」后回来刷新。');
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 12, height: '100%' } },
        h('div', { style: bar },
          h('span', { style: title }, '记忆图谱'),
          h('button', { style: btn, onClick: () => send('mem:reload') }, '刷新'),
          h('button', { style: btn, onClick: () => send('mem:settings') }, '设置'),
          src ? h('a', {
            href: src.replace('?embed=1', ''), target: '_blank', rel: 'noreferrer',
            style: { ...btn, textDecoration: 'none' },
          }, '新窗口打开') : null,
        ),
        src ? h('iframe', {
          ref: frame, src, title: '记忆图谱',
          style: { flex: 1, width: '100%', minHeight: 520, border: '1px solid rgba(255,255,255,.14)', borderRadius: 12, background: 'transparent' },
        }) : h('div', { style: { width: '100%', height: 260, borderRadius: 12, background: 'rgba(255,255,255,.06)', border: '1px solid #272727' } }),
      );
    }

    class Boundary extends React.Component {
      constructor(props) { super(props); this.state = { err: null }; }
      static getDerivedStateFromError(err) { return { err }; }
      render() {
        if (this.state.err) {
          return h('div', { style: { padding: 16, fontSize: 14 } },
            '记忆视图加载失败：' + String((this.state.err && this.state.err.message) || this.state.err));
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
