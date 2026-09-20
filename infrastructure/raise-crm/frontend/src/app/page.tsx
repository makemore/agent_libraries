export default function Home() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center p-24">
      <main className="flex flex-col items-center gap-8 text-center">
        <h1 className="text-4xl font-bold tracking-tight">
          Next.js Starter Template
        </h1>
        <p className="text-lg text-muted-foreground max-w-2xl">
          A production-ready starter with Next.js 16, React 19, Tailwind CSS v4, and shadcn/ui.
          Start building your next project with modern tools and best practices.
        </p>
        <div className="flex gap-4">
          <a
            href="https://nextjs.org/docs"
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-lg bg-primary px-6 py-3 text-primary-foreground hover:bg-primary/90 transition-colors"
          >
            Read the docs
          </a>
          <a
            href="https://ui.shadcn.com"
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-lg border border-border px-6 py-3 hover:bg-accent transition-colors"
          >
            shadcn/ui
          </a>
        </div>
      </main>
    </div>
  );
}
