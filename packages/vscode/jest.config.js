module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  testMatch: ['**/src/__tests__/**/*.test.ts'],
  moduleNameMapper: {
    vscode: '<rootDir>/src/__mocks__/vscode.ts',
  },
};
