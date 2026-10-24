import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "LexVault · 律师阅卷工作台", description: "本地卷宗、引用核验与审校工作台" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
