window.__ModuleLoader__.load({
  id: '@local/memory-ui',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useState, useEffect, useRef } = React;

    const PORTS = [8790, 8791, 8792, 8793, 8794, 8795, 8796, 8797, 8798, 8799];
    const btn = {
      padding: '8px 12px', borderRadius: 8, border: '1px solid #272727', background: 'rgba(255,255,255,.06)',
      color: 'inherit', fontSize: 14, lineHeight: '20px', cursor: 'pointer',
      transition: 'background-color 300ms cubic-bezier(.22,1,.36,1)',
    };

    function MemorySection() {
      const [src, setSrc] = useState('');
      const [base, setBase] = useState('');
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
          // 用 <img> 探活而不是 fetch：Electron/沙箱页面的 Origin 是 "null"，
          // fetch 会被 CORS 拦掉（服务其实活着），而图片请求不受 CORS 约束。
          const beacon = (p) => new Promise((resolve) => {
            const img = new Image();
            let done = false;
            const finish = (ok) => {
              if (done) return;
              done = true;
              img.onload = img.onerror = null;
              clearTimeout(timer);
              resolve(ok);
            };
            const timer = setTimeout(() => finish(false), 1200);
            img.onload = () => finish(true);
            img.onerror = () => finish(false);
            img.src = 'http://127.0.0.1:' + p + '/favicon.ico?_=' + Date.now();
          });

          for (const p of PORTS) {
            const up = await beacon(p);
            if (!up) continue;
            if (alive) {
              setBase('http://127.0.0.1:' + p + '/');
              setSrc('http://127.0.0.1:' + p + '/?embed=1' + q);
            }
            return;
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
        return h('div', { style: { padding: 16, fontSize: 14, lineHeight: '22px' } },
          h('div', { style: { fontWeight: 600, marginBottom: 6 } }, '记忆面板服务没有运行'),
          h('div', { style: { opacity: .7, fontSize: 13 } }, '在终端执行下面任一条，然后点「重试」：'),
          h('pre', { style: { margin: '8px 0', padding: '8px 10px', borderRadius: 8,
            background: 'rgba(255,255,255,.06)', fontSize: 12 } }, 'mem serve --open'),
          h('button', { style: btn, onClick: () => { setFailed(false); window.location.reload(); } }, '重试'));
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 12, height: '100%' } },
        h('div', { style: bar },
          h('span', { style: title }, '记忆星云'),
          h('button', { style: btn, onClick: () => send('mem:reload') }, '刷新'),
          h('button', { style: btn, onClick: () => send('mem:settings') }, '设置'),
          src ? h('a', {
            href: base || 'http://127.0.0.1:8790/', target: '_blank', rel: 'noreferrer',
            style: { ...btn, textDecoration: 'none' },
          }, '新窗口打开') : null,
        ),
        src ? h('iframe', {
          ref: frame, src, title: '记忆星云',
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
