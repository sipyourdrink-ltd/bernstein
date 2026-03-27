import { BernsteinClient } from '../BernsteinClient';

describe('BernsteinClient', () => {
  describe('constructor', () => {
    it('strips trailing slash from baseUrl', () => {
      const client = new BernsteinClient('http://localhost:8052/', '');
      expect(client.baseUrl).toBe('http://localhost:8052');
    });

    it('preserves baseUrl without trailing slash', () => {
      const client = new BernsteinClient('http://localhost:8052', '');
      expect(client.baseUrl).toBe('http://localhost:8052');
    });
  });

  describe('headers()', () => {
    it('includes Authorization when token provided', () => {
      const client = new BernsteinClient('http://localhost:8052', 'mytoken');
      expect(client.headers()['Authorization']).toBe('Bearer mytoken');
    });

    it('omits Authorization when token is empty', () => {
      const client = new BernsteinClient('http://localhost:8052', '');
      expect(client.headers()['Authorization']).toBeUndefined();
    });

    it('always includes Content-Type', () => {
      const client = new BernsteinClient('http://localhost:8052', '');
      expect(client.headers()['Content-Type']).toBe('application/json');
    });
  });
});
