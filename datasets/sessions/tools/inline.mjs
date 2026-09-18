import {readFileSync,writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
const [dist,out]=process.argv.slice(2);
let html=readFileSync(resolve(dist,'index.html'),'utf8');
html=html.replace(/<script[^>]*src="([^"]+)"[^>]*><\/script>/g,(_,file)=>'<script type="module">'+readFileSync(resolve(dist,'.'+file),'utf8').replaceAll('</script','<\\/script')+'</script>');
html=html.replace(/<link[^>]*href="([^"]+\.css)"[^>]*>/g,(_,file)=>'<style>'+readFileSync(resolve(dist,'.'+file),'utf8')+'</style>');
writeFileSync(out,html);
