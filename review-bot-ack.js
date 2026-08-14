function regenerateBody(prBody) {
  const markers = prBody.match(/<!-- bot-ack: <id> reason=.*? -->/gs);
  if (markers) {
    markers.forEach(marker => prBody = prBody.replace(marker, ''));
  }
  return prBody;
}