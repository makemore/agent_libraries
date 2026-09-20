# Next.js Starter Template

A production-ready Next.js starter template with modern tooling and best practices.

## 🚀 Quick Start

```bash
# Create new project
cp -r ~/Projects/nextjs-starter ~/Projects/my-new-project
cd ~/Projects/my-new-project
./setup.sh

# Start developing
npm run dev
```

**That's it!** Open [http://localhost:3000](http://localhost:3000) 🎉

## 📦 What's Included

- ⚡️ **Next.js 16.0.7** - Latest with Turbopack & security patches
- ⚛️ **React 19.2.1** - Latest with security patches (CVE-2025-55182)
- 🎨 **Tailwind CSS v4** - Modern CSS with OKLCH colors
- 🧩 **shadcn/ui** - Pre-configured, ready to use
- 📝 **TypeScript** - Full type safety
- 🎯 **ESLint** - Next.js recommended config
- 🔒 **0 Vulnerabilities** - Fully patched and secure

## 📚 Documentation

- **[QUICKSTART.md](./QUICKSTART.md)** - Get started in 30 seconds
- **[USAGE.md](./USAGE.md)** - Detailed usage guide
- **[COMPARISON.md](./COMPARISON.md)** - vs Django Cookiecutter

## 🎨 Adding Components

```bash
# Add shadcn/ui components
npx shadcn@latest add button
npx shadcn@latest add card
npx shadcn@latest add dialog
```

## 📝 Available Scripts

```bash
npm run dev      # Development server
npm run build    # Production build
npm start        # Production server
npm run lint     # Run ESLint
```

## 📁 Project Structure

```
nextjs-starter/
├── src/
│   ├── app/
│   │   ├── globals.css      # Tailwind + theme
│   │   ├── layout.tsx       # Root layout
│   │   └── page.tsx         # Home page
│   ├── components/ui/       # shadcn/ui components
│   └── lib/utils.ts         # Utilities (cn helper)
├── components.json          # shadcn/ui config
├── setup.sh                 # Setup script
└── README.md
```

## 🎯 Features

### Modern Stack
- Next.js 16 with Turbopack for fast builds
- React 19 with latest features
- Tailwind CSS v4 with modern CSS features
- TypeScript for type safety

### Developer Experience
- Hot Module Replacement
- Fast Refresh
- TypeScript IntelliSense
- ESLint integration

### Production Ready
- Optimized builds
- Security patches applied
- Zero vulnerabilities
- Best practices configured

### Customizable
- Clean, neutral theme
- OKLCH color system
- Dark mode ready
- Easy to customize

## 🔒 Security

This template includes the latest security patches:
- **CVE-2025-66478** (Next.js) - Patched in 16.0.7
- **CVE-2025-55182** (React) - Patched in 19.2.1

## 🚀 Deployment

### Vercel (Recommended)
```bash
npm install -g vercel
vercel
```

### Other Platforms
```bash
npm run build
npm start
```

## 💡 Tips

1. **Use the `cn` utility** for conditional classes
2. **Create `.env.local`** for environment variables
3. **Organize components** in `src/components/`
4. **Use TypeScript** for better DX

## 📖 Learn More

- [Next.js Documentation](https://nextjs.org/docs)
- [Tailwind CSS Documentation](https://tailwindcss.com/docs)
- [shadcn/ui Documentation](https://ui.shadcn.com)
- [React Documentation](https://react.dev)

## 🤝 Similar to Django Cookiecutter

This template provides the same quick-start experience as your Django cookiecutter, but uses JavaScript-native tools:

```bash
# Django
cookiecutter ../django_starter/django_starter_cookiecutter

# Next.js (equivalent)
cp -r ~/Projects/nextjs-starter ~/Projects/my-project && cd ~/Projects/my-project && ./setup.sh
```

See [COMPARISON.md](./COMPARISON.md) for detailed comparison.

## 📄 License

MIT License - use freely for any project!

---

**Happy coding! 🎉**
