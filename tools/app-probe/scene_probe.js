const { chromium } = require(process.argv[2]);
(async () => {
  const b = await chromium.launch({ channel: 'chrome', headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const p = await b.newPage({ viewport: { width: 1280, height: 800 } });
  await p.goto('file:///' + process.argv[3].replace(/\\/g, '/'));
  await p.waitForTimeout(1000);
  await p.click('#start-btn');
  await p.waitForTimeout(4000);
  console.log(await p.evaluate(() => {
    let meshes = 0; scene.traverse(o => { if (o.isMesh) meshes++; });
    const box = new THREE.Box3().setFromObject(scene);
    return JSON.stringify({ meshes, cam: camera.position, camRot: camera.rotation.toArray().slice(0,3), look: camera.getWorldDirection(new THREE.Vector3()),
      player: player && player.group ? player.group.position : null, sceneMin: box.min, sceneMax: box.max,
      calls: renderer.info.render.calls, tris: renderer.info.render.triangles, gl: renderer.getContext().getParameter(renderer.getContext().VERSION) });
  }));
  await b.close();
})();
