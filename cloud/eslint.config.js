import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: [".wrangler", "coverage", "node_modules"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { "argsIgnorePattern": "^_", "varsIgnorePattern": "^_" }]
    }
  },
  {
    files: ["public/*.js"],
    languageOptions: { globals: globals.browser },
    rules: {
      "@typescript-eslint/no-unused-vars": "off",
      "no-useless-assignment": "off"
    }
  },
  {
    files: ["public/*_sw.js"],
    languageOptions: { globals: globals.serviceworker }
  },
  {
    files: ["test/**/*.mjs"],
    languageOptions: { globals: globals.node },
    rules: { "no-empty": "off" }
  }
);
