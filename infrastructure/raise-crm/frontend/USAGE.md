# Usage Guide

## Creating a New Project from This Template

### Method 1: Copy and Setup (Recommended)

This is the simplest and fastest method:

```bash
# Copy the template
cp -r ~/Projects/nextjs-starter ~/Projects/my-new-project
cd ~/Projects/my-new-project

# Run setup script
./setup.sh
```

### Method 2: Using degit with GitHub

After pushing this template to GitHub:

```bash
# Clone from GitHub (no git history)
npx degit yourusername/nextjs-starter my-new-project
cd my-new-project
./setup.sh
```

### Method 3: Manual Setup

```bash
# Copy the template
cp -r ~/Projects/nextjs-starter ~/Projects/my-new-project
cd ~/Projects/my-new-project

# Remove git history
rm -rf .git

# Update package.json name manually
# Edit package.json and change "name": "nextjs-starter" to your project name

# Install dependencies
npm install

# Initialize git (optional)
git init
git add .
git commit -m "Initial commit"

# Start development
npm run dev
```

## What the Setup Script Does

The `setup.sh` script:
1. ✅ Prompts for your project name
2. ✅ Updates `package.json` with your project name
3. ✅ Installs all dependencies
4. ✅ Initializes a new git repository
5. ✅ Creates initial commit

## Adding shadcn/ui Components

The template is pre-configured for shadcn/ui. Add components:

```bash
# Add individual components
npx shadcn@latest add button
npx shadcn@latest add card
npx shadcn@latest add dialog
npx shadcn@latest add input
npx shadcn@latest add label

# Add multiple at once
npx shadcn@latest add button card dialog input label
```

Components will be added to `src/components/ui/`.

## Common Customizations

### 1. Change App Title and Description

Edit `src/app/layout.tsx`:

```typescript
export const metadata: Metadata = {
  title: "Your App Name",
  description: "Your app description",
};
```

### 2. Customize Theme Colors

Edit `src/app/globals.css` - look for the `:root` and `.dark` sections.

### 3. Add Environment Variables

Create `.env.local`:

```bash
NEXT_PUBLIC_API_URL=https://api.example.com
DATABASE_URL=postgresql://...
```

### 4. Add Custom Fonts

Edit `src/app/layout.tsx` to add Google Fonts or local fonts.

### 5. Configure Deployment

#### Vercel (Recommended)
```bash
npm install -g vercel
vercel
```

#### Other Platforms
- Build: `npm run build`
- Start: `npm start`
- Output: `.next` directory

## Project Structure Explained

```
src/
├── app/                    # Next.js App Router
│   ├── globals.css        # Global styles + Tailwind
│   ├── layout.tsx         # Root layout (wraps all pages)
│   └── page.tsx           # Home page (/)
├── components/
│   └── ui/                # shadcn/ui components
├── lib/
│   └── utils.ts           # Utility functions (cn helper)
└── hooks/                 # Custom React hooks (create as needed)
```

## Tips & Best Practices

### 1. Use the `cn` Utility

For conditional classes:

```typescript
import { cn } from "@/lib/utils";

<div className={cn(
  "base-classes",
  condition && "conditional-classes",
  variant === "primary" && "primary-classes"
)} />
```

### 2. Organize Components

```
src/components/
├── ui/              # shadcn/ui components
├── layout/          # Layout components (Header, Footer, etc.)
├── features/        # Feature-specific components
└── shared/          # Shared/common components
```

### 3. Use TypeScript

Always define types for props:

```typescript
interface ButtonProps {
  label: string;
  onClick: () => void;
  variant?: "primary" | "secondary";
}

export function Button({ label, onClick, variant = "primary" }: ButtonProps) {
  // ...
}
```

### 4. Environment Variables

- `NEXT_PUBLIC_*` - Exposed to browser
- Others - Server-side only

## Troubleshooting

### Port Already in Use

```bash
# Use a different port
npm run dev -- -p 3001
```

### Clear Next.js Cache

```bash
rm -rf .next
npm run dev
```

### TypeScript Errors

```bash
# Restart TypeScript server in your editor
# Or run type check:
npx tsc --noEmit
```

## Updating Dependencies

```bash
# Check for updates
npm outdated

# Update all to latest
npm update

# Update specific package
npm install next@latest
```

## Updating the Template

To pull updates from the template into an existing project:

```bash
cd ~/Projects/my-existing-project

# Add template as remote
git remote add template ~/Projects/nextjs-starter

# Fetch and merge updates
git fetch template
git merge template/main --allow-unrelated-histories
```

## Getting Help

- [Next.js Docs](https://nextjs.org/docs)
- [Tailwind CSS Docs](https://tailwindcss.com/docs)
- [shadcn/ui Docs](https://ui.shadcn.com)
- [React Docs](https://react.dev)
