# Quick Start Guide

## 🚀 Create a New Project (One Command)

```bash
# Copy template and run setup
cp -r ~/Projects/nextjs-starter ~/Projects/my-new-project && cd ~/Projects/my-new-project && ./setup.sh
```

## 📦 What You Get

- ✅ Next.js 16.0.7 (latest, security patched)
- ✅ React 19.2.1 (latest, security patched)
- ✅ Tailwind CSS v4 (latest)
- ✅ TypeScript configured
- ✅ shadcn/ui ready
- ✅ ESLint configured
- ✅ 0 vulnerabilities

## 🎨 Add UI Components

```bash
# Add shadcn/ui components
npx shadcn@latest add button
npx shadcn@latest add card
npx shadcn@latest add dialog
npx shadcn@latest add input
```

## 🔧 Common Commands

```bash
npm run dev      # Start dev server (http://localhost:3000)
npm run build    # Build for production
npm start        # Start production server
npm run lint     # Run ESLint
```

## 📁 Project Structure

```
my-new-project/
├── src/
│   ├── app/              # Pages and routes
│   ├── components/ui/    # shadcn/ui components
│   └── lib/              # Utilities
├── package.json
└── README.md
```

## 🎯 Next Steps

1. **Customize metadata**: Edit `src/app/layout.tsx`
2. **Change theme**: Edit `src/app/globals.css`
3. **Add pages**: Create files in `src/app/`
4. **Add components**: Use shadcn/ui or create your own

## 💡 Tips

- Use `cn()` utility for conditional classes
- Environment variables: Create `.env.local`
- Deploy to Vercel: `npx vercel`

## 📚 Documentation

- [Full Usage Guide](./USAGE.md)
- [Next.js Docs](https://nextjs.org/docs)
- [shadcn/ui Docs](https://ui.shadcn.com)

---

**That's it! You're ready to build. 🎉**
