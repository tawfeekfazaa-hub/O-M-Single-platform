import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AQ O&M Platform",
  description:
    "Unified solar monitoring & maintenance for Arabian Qudra Solar",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
