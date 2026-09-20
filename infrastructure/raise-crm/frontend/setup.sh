#!/bin/bash

# Next.js Starter Setup Script
# This script will delete itself after running

set -e

echo "🚀 Setting up your Next.js project..."
echo ""

# Check if package.json exists
if [ ! -f "package.json" ]; then
    echo "❌ Error: package.json not found. Are you in the right directory?"
    exit 1
fi

# Get project name from user
read -p "📝 Enter your project name (default: my-nextjs-app): " PROJECT_NAME
PROJECT_NAME=${PROJECT_NAME:-my-nextjs-app}

# Validate project name (alphanumeric, hyphens, underscores only)
if ! [[ "$PROJECT_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "❌ Error: Project name can only contain letters, numbers, hyphens, and underscores"
    exit 1
fi

# Update package.json with new project name
echo "📦 Updating package.json..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    sed -i '' "s/\"name\": \"nextjs-starter\"/\"name\": \"$PROJECT_NAME\"/" package.json
else
    # Linux
    sed -i "s/\"name\": \"nextjs-starter\"/\"name\": \"$PROJECT_NAME\"/" package.json
fi

# Install dependencies
echo ""
echo "📥 Installing dependencies..."
npm install

# Initialize git if not already initialized
if [ ! -d ".git" ]; then
    echo ""
    echo "🔧 Initializing git repository..."
    git init
    git add .
    git commit -m "Initial commit from nextjs-starter template"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "🎉 Your project '$PROJECT_NAME' is ready!"
echo ""
echo "Next steps:"
echo "  1. Run 'npm run dev' to start the development server"
echo "  2. Add shadcn/ui components: 'npx shadcn@latest add button'"
echo "  3. Start building your app!"
echo ""

# Self-destruct: Remove this setup script
echo "🗑️  Removing setup script..."
rm -- "$0"

echo "✨ All done!"
