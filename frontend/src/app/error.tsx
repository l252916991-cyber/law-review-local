'use client';
export default function ErrorPage({ reset }: { reset: () => void }) {
  return <main><h1>页面暂时无法显示</h1><p role="alert">接口返回异常或连接中断，请重试。</p><button onClick={reset}>重新加载</button></main>;
}
