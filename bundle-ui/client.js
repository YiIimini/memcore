window.__ModuleLoader__.load({
  id: '@local/memory-ui',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useState, useEffect } = React;

    const PORTS = [8790, 8791, 8792, 8793, 8794, 8795];
    const FRAME = { flex: 1, width: '100%', minHeight: 520, border: '1px solid #272727', borderRadius: 12, background: '#000' };

    function MemorySection() {
      const [src, setSrc] = useState('');
      const [failed, setFailed] = useState(false);

      useEffect(() => {
        let alive = true;
        (async () => {
          for (const p of PORTS) {
            try {
              const r = await fetch('http://127.0.0.1:' + p + '/api/graph?limit=1');
              if (r.ok) { if (alive) setSrc('http://127.0.0.1:' + p + '/?embed=1'); return; }
            } catch (e) { /* try next port */ }
          }
          if (alive) setFailed(true);
        })();
        return () => { alive = false; };
      }, []);

      if (failed) {
        return h('div', { style: { padding: 16, fontSize: 14, lineHeight: '20px' } },
          '记忆面板服务没有运行。打开终端执行 mem serve，或双击桌面的「记忆面板」后回来刷新。');
      }
      if (!src) {
        return h('div', { style: { padding: 16, display: 'flex', flexDirection: 'column', gap: 8 } },
          h('div', { style: { width: 180, height: 12, borderRadius: 6, background: '#1F1F1F' } }),
          h('div', { style: { width: '100%', height: 220, borderRadius: 12, background: '#181818', border: '1px solid #272727' } }));
      }
      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 12, height: '100%' } },
        h('iframe', { src, style: FRAME, title: '记忆图谱' }));
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
