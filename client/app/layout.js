import './globals.css';

export const metadata = {
  title: 'Cubicle — Local-First AI Agent Chat',
  description: 'Local-first AI agent chat app built with Next.js and FastAPI. Works with OpenRouter, Ollama, LM Studio, llama.cpp and any OpenAI-compatible server.',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning={true}>
      <body className="bg-background text-foreground antialiased" suppressHydrationWarning={true}>
        {children}
      </body>
    </html>
  );
}
