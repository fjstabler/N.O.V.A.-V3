/* eslint-env node */
module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  plugins: ['@typescript-eslint', 'react-hooks'],
  extends: ['eslint:recommended', 'plugin:@typescript-eslint/recommended'],
  ignorePatterns: ['dist', 'dist-electron', 'node_modules', '*.cjs'],
  rules: {
    'react-hooks/rules-of-hooks': 'error',
    'react-hooks/exhaustive-deps': 'warn',
    '@typescript-eslint/no-unused-vars': [
      'error',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
    ],
    // The bridge deals in `unknown` payloads that are narrowed at the edges;
    // banning explicit `any` there would only push it into casts.
    '@typescript-eslint/no-explicit-any': 'off',
    'no-console': ['warn', { allow: ['info', 'warn', 'error'] }],
    eqeqeq: ['error', 'smart'],
    'prefer-const': 'error',
  },
  overrides: [
    {
      // Ambient declaration files exist to be merged into global scope; every
      // interface in them is "unused" by definition.
      files: ['*.d.ts'],
      rules: { '@typescript-eslint/no-unused-vars': 'off' },
    },
  ],
};
